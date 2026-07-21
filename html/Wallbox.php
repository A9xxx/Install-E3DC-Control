<?php
/* =====================================================================
   E3DC-Control - Wallbox.php (Hauptseite: Direkt + Automatik)
   ===================================================================== */

// Pfade und Config-Datei-Pfad VOR der POST-Logik definieren
require_once __DIR__ . '/helpers.php';
require_once __DIR__ . '/wallbox_transaction.php';
$wallboxAuthIsAjax =
    (($_SERVER['REQUEST_METHOD'] ?? '') === 'POST')
    || (isset($_GET['ajax']) && $_GET['ajax'] == '1')
    || (isset($_SERVER['HTTP_X_REQUESTED_WITH']) && strtolower($_SERVER['HTTP_X_REQUESTED_WITH']) === 'xmlhttprequest');
requireWebAuth($wallboxAuthIsAjax);

$paths = getInstallPaths();
$install_user = !empty($paths['valid']) ? (string)$paths['install_user'] : '';
$base_path = !empty($paths['valid'])
    ? rtrim((string)$paths['install_path'], '/') . '/'
    : '/var/www/html/data/.invalid-install-context/';
$config_file = $base_path . 'e3dc.config.txt';
$message = '';
if (($_SERVER['REQUEST_METHOD'] ?? 'GET') === 'POST' && empty($paths['valid'])) {
    http_response_code(503);
    echo errorMessage('Wallbox-Aktion nicht ausgeführt', getInstallContextDiagnostic());
    exit;
}

function normalizeWallboxVehicleSelection($value) {
    $value = trim((string)$value);
    return $value === '' ? '__none' : $value;
}

function wallboxTruthy($value) {
    if (is_bool($value)) return $value;
    if ($value === null || $value === '') return false;
    if (is_numeric($value)) return ((float)$value) != 0.0;
    return in_array(strtolower(trim((string)$value)), ['1', 'true', 'yes', 'on', 'locked', 'connected'], true);
}

function wallboxWantsConfigJsonResponse() {
    return (string)($_POST['response_format'] ?? '') === 'json';
}

function wallboxEmitConfigJsonResponse($result, $operation) {
    $ok = !empty($result['success']);
    $code = preg_replace('/[^a-z0-9_\-]/i', '', (string)($result['code'] ?? ($ok ? 'ok' : 'unknown'))) ?: 'unknown';
    if (!$ok) {
        http_response_code(500);
        wallboxLogConfigFailure($operation, $code);
    }
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(['ok' => $ok, 'code' => $code], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

function normalizeWallboxModeValue($value) {
    $mode = (int)$value;
    if ($mode === 0) return '0';
    if (in_array($mode, [1, 2], true)) return '2';
    if ($mode === 3) return '3';
    if ($mode === 6) return '3';
    if (in_array($mode, [4, 9, 10], true)) return '4';
    if (in_array($mode, [5, 11], true)) return '5';
    if ($mode === 12) return '12';
    return '2';
}

function sanitizeWallboxPercentValue($value, $default = 80) {
    $pct = (int)round((float)str_replace(',', '.', (string)($value ?? $default)));
    return (string)max(0, min(100, $pct));
}

function readE3dcWallboxDischargeFloorSoc() {
    foreach ([
        '/var/www/html/ramdisk/live_data_py.json',
        '/var/www/html/ramdisk/wallbox_native.json',
    ] as $path) {
        if (!is_file($path)) continue;
        $data = json_decode((string)@file_get_contents($path), true);
        if (!is_array($data)) continue;
        $raw = $data['e3dc_wb_discharge_bat_until_soc'] ?? null;
        if ($raw === null || $raw === '' || !is_numeric($raw)) continue;
        $floor = (float)$raw;
        if ($floor > 0.0 && $floor <= 100.0) {
            return $floor;
        }
    }
    return null;
}

function sanitizeWallboxHouseReserveValue($value, $default = 70) {
    $reserve = (int)sanitizeWallboxPercentValue($value, $default);
    $e3dcFloor = readE3dcWallboxDischargeFloorSoc();
    if ($e3dcFloor !== null && $e3dcFloor > $reserve) {
        $reserve = (int)ceil($e3dcFloor);
    }
    return (string)max(0, min(100, $reserve));
}

function wallboxHouseReserveFloorNotice($configuredValue) {
    $configured = (int)sanitizeWallboxPercentValue($configuredValue, 70);
    $e3dcFloor = readE3dcWallboxDischargeFloorSoc();
    if ($e3dcFloor === null || $e3dcFloor <= $configured + 0.05) {
        return '';
    }
    return sprintf(
        'E3DC-Untergrenze %.1f%% hebt die gespeicherte Hausakku-Reserve %.0f%% auf %.0f%% an.',
        $e3dcFloor,
        $configured,
        ceil($e3dcFloor)
    );
}

function sanitizeWallboxKwhValue($value, $default = 20.0) {
    $kwh = (float)str_replace(',', '.', (string)($value ?? $default));
    if (!is_finite($kwh)) $kwh = $default;
    return rtrim(rtrim(number_format(max(0.0, min(200.0, $kwh)), 1, '.', ''), '0'), '.');
}

function sanitizeWallboxKwValue($value, $default = 11.0) {
    $kw = (float)str_replace(',', '.', (string)($value ?? $default));
    if (!is_finite($kw) || $kw <= 0) $kw = $default;
    return rtrim(rtrim(number_format(max(0.1, min(44.0, $kw)), 1, '.', ''), '0'), '.');
}

function normalizeWallboxSimpleEnergyMode($value) {
    $value = strtolower(trim((string)$value));
    if (in_array($value, ['pv', 'pv_battery', 'grid_price'], true)) {
        return $value;
    }
    return 'pv';
}

function normalizeWallboxSimpleChargeIntent($value) {
    $value = strtolower(trim((string)$value));
    if ($value === 'auto') {
        return 'surplus';
    }
    if (in_array($value, ['surplus', 'scheduled', 'instant', 'off'], true)) {
        return $value;
    }
    return 'surplus';
}

function normalizeWallboxSimpleTargetUnit($value) {
    $value = strtolower(trim((string)$value));
    return $value === 'kwh' ? 'kwh' : 'soc';
}

function normalizeWallboxSimpleChoice($energyMode, $chargeIntent) {
    $energyMode = normalizeWallboxSimpleEnergyMode($energyMode);
    $chargeIntent = normalizeWallboxSimpleChargeIntent($chargeIntent);
    if ($energyMode === 'grid_price' && $chargeIntent === 'surplus') {
        $chargeIntent = 'scheduled';
    }
    return [$energyMode, $chargeIntent];
}

function simpleWallboxNativeMode($energyMode, $chargeIntent) {
    [$energyMode, $chargeIntent] = normalizeWallboxSimpleChoice($energyMode, $chargeIntent);
    if ($chargeIntent === 'off') return '0';
    if ($energyMode === 'grid_price') return '5';
    if ($energyMode === 'pv_battery' && $chargeIntent === 'scheduled') return '12';
    if ($energyMode === 'pv_battery') return '4';
    return '2';
}

function simpleWallboxObserveStoragePolicy($energyMode, $chargeIntent) {
    [$energyMode, $chargeIntent] = normalizeWallboxSimpleChoice($energyMode, $chargeIntent);
    return ($chargeIntent === 'off' && $energyMode === 'pv_battery') ? 'reserve' : 'curve';
}

function normalizeWallboxObserveStoragePolicy($value) {
    $value = strtolower(trim((string)$value));
    return in_array($value, ['reserve', 'pv_battery', 'battery', 'akku', 'wbminsoc', 'floor', '1', 'true', 'yes', 'on'], true)
        ? 'reserve'
        : 'curve';
}

function normalizeWallboxSimpleReadyTime($value) {
    $value = trim((string)$value);
    if (preg_match('/^([01][0-9]|2[0-3]):[0-5][0-9]$/', $value)) {
        return $value;
    }
    return '07:00';
}

function normalizeWallboxPriceLimitValue($value, $default = 0.0) {
    $limit = (float)str_replace(',', '.', (string)($value ?? $default));
    if (!is_finite($limit)) $limit = $default;
    return (string)max(0.0, min(200.0, $limit));
}

function readWallboxManualSocValue($wbId) {
    $wbId = max(1, min(2, (int)$wbId));
    foreach ([
        "/var/www/html/ramdisk/manual_soc_wb{$wbId}.json",
        $wbId === 1 ? '/var/www/html/tmp/manual_soc.json' : '',
    ] as $path) {
        if ($path === '' || !is_file($path)) continue;
        $data = json_decode((string)@file_get_contents($path), true);
        if (!is_array($data) || !isset($data['soc']) || !is_numeric($data['soc'])) continue;
        return sanitizeWallboxPercentValue($data['soc'], 0);
    }
    return '';
}

function buildWallboxManualSocSample($wbId, $socValue, $carId, $carName, $capacity, $source = 'manual_start_soc') {
    $wbId = max(1, min(2, (int)$wbId));
    $socText = trim((string)$socValue);
    if ($socText === '') return false;
    $soc = (float)str_replace(',', '.', $socText);
    if (!is_finite($soc)) return false;
    $soc = max(0.0, min(100.0, $soc));
    return [
        'soc' => $soc,
        'car_id' => trim((string)$carId),
        'name' => trim((string)$carName),
        'capacity' => (float)str_replace(',', '.', (string)$capacity),
        'wb' => $wbId,
        'source' => $source,
        'plugged' => true,
        'ts' => time(),
    ];
}

function normalizeWallboxDepartureTime($value) {
    $value = trim((string)$value);
    if (preg_match('/^([01][0-9]|2[0-3]):[0-5][0-9]$/', $value)) {
        return $value;
    }
    return '06:30';
}

function normalizeWallboxDepartureWindowHours($value) {
    $hours = (float)str_replace(',', '.', (string)$value);
    if (!is_finite($hours) || $hours <= 0) $hours = 3.0;
    $hours = max(1.0, min(36.0, $hours));
    return rtrim(rtrim(number_format($hours, 1, '.', ''), '0'), '.');
}

function clearWallboxManualPauseOnModeChange(&$updates, $wbId, $newMode, $previousMode) {
    $wbId = max(1, min(2, (int)$wbId));
    if ((string)$newMode === (string)$previousMode) return false;
    $updates["wb{$wbId}_manual_pause"] = '0';
    return true;
}

function compactVehicleIdentifierWallbox($value) {
    return preg_replace('/[^a-z0-9]/', '', strtolower(trim((string)$value)));
}

function wallboxVehicleAliases($vehicle) {
    if (!is_array($vehicle)) return [];
    $aliases = [];
    foreach (['id', 'profile_id', 'vehicle_id', 'vehicle_mac', 'mac', 'rfid', 'rfid_tag', 'cloud_vehicle_id'] as $key) {
        $compact = compactVehicleIdentifierWallbox($vehicle[$key] ?? '');
        if ($compact !== '') $aliases[$compact] = true;
    }
    return array_keys($aliases);
}

function wallboxSavedCarMatchesSelection($car, $selection) {
    $probe = compactVehicleIdentifierWallbox($selection);
    if ($probe === '' || !is_array($car)) return false;
    foreach (wallboxVehicleAliases($car) as $alias) {
        if ($alias === $probe) return true;
    }
    return false;
}

function canonicalWallboxVehicleSelection($value, $savedCars) {
    $value = normalizeWallboxVehicleSelection($value);
    if ($value === '__none' || $value === 'none') return $value;
    foreach (($savedCars ?? []) as $car) {
        if (wallboxSavedCarMatchesSelection($car, $value) && !empty($car['id'])) {
            return (string)$car['id'];
        }
    }
    return $value;
}

function readJsonArrayWallbox($path, $default = []) {
    if (!file_exists($path)) return $default;
    $data = @json_decode(@file_get_contents($path), true);
    return is_array($data) ? $data : $default;
}

function wallboxCurrentPlanSlot($wbId, $nowTs = null) {
    $wbId = max(1, min(2, (int)$wbId));
    $nowTs = $nowTs ?? time();
    $files = [
        "/var/www/html/ramdisk/native_wallbox_schedule_wb{$wbId}.json",
        '/var/www/html/ramdisk/native_wallbox_schedule.json',
    ];
    foreach ($files as $path) {
        $slots = readJsonArrayWallbox($path, []);
        if (!is_array($slots)) continue;
        foreach ($slots as $slot) {
            if (!is_array($slot)) continue;
            if (isset($slot['wb_id']) && (int)$slot['wb_id'] !== $wbId) continue;
            if (!isset($slot['wb_id']) && $wbId !== 1) continue;
            $slotTs = (int)($slot['ts'] ?? 0);
            if ($slotTs > 0 && $slotTs <= $nowTs && $nowTs < ($slotTs + 900)) {
                return [
                    'active' => true,
                    'wb' => $wbId,
                    'slot_ts' => $slotTs,
                    'slot_end' => $slotTs + 900,
                    'label' => 'WB' . $wbId . ' ' . date('H:i', $slotTs) . '-' . date('H:i', $slotTs + 900),
                ];
            }
        }
    }
    return ['active' => false, 'wb' => $wbId];
}

function wallboxNormalizeOpenWbProHost($value) {
    $value = trim((string)$value);
    if ($value === '' || in_array(strtolower($value), ['0', '0.0.0.0', 'none', 'false', 'off', 'disabled'], true)) {
        return '';
    }
    if (preg_match('~^https?://~i', $value)) {
        $parts = parse_url($value);
        if (!is_array($parts) || empty($parts['host'])) return '';
        $path = trim((string)($parts['path'] ?? ''), '/');
        if ($path !== '') return '';
        $host = $parts['host'];
        if (isset($parts['port'])) $host .= ':' . (int)$parts['port'];
        $value = $host;
    }
    if (strpos($value, '/') !== false || strpos($value, '\\') !== false || strpos($value, '@') !== false) {
        return '';
    }
    if (!preg_match('/^[A-Za-z0-9._:-]+$/', $value)) {
        return '';
    }
    return $value;
}

function wallboxOpenWbProUpdateTargets($cfg, $onlyWbId = null) {
    if (!is_array($cfg)) return [];
    $targets = [];
    for ($wb = 1; $wb <= 2; $wb++) {
        if ($onlyWbId !== null && (int)$onlyWbId !== $wb) continue;
        $typeKey = $wb === 1 ? 'wb_native_type' : 'wb_native_type2';
        $ipKey = $wb === 1 ? 'wb_native_ip' : 'wb_native_ip2';
        if (normalizeWallboxTypeConfig($cfg[$typeKey] ?? '') !== 'openwb_pro') continue;
        $host = wallboxNormalizeOpenWbProHost($cfg[$ipKey] ?? '');
        if ($host === '') continue;
        $targets[$wb] = [
            'wb' => $wb,
            'host' => $host,
            'label' => 'WB' . $wb . ' openWB Pro',
        ];
    }
    return $targets;
}

function wallboxPostOpenWbProUpdate($host) {
    $host = wallboxNormalizeOpenWbProHost($host);
    if ($host === '') {
        return ['success' => false, 'message' => 'Ungültige openWB-Pro-Adresse'];
    }
    $url = 'http://' . $host . '/connect.php';
    $body = http_build_query(['update' => '1']);

    if (function_exists('curl_init')) {
        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $body,
            CURLOPT_HTTPHEADER => ['Content-Type: application/x-www-form-urlencoded'],
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CONNECTTIMEOUT => 4,
            CURLOPT_TIMEOUT => 8,
        ]);
        $response = curl_exec($ch);
        $err = curl_error($ch);
        $code = (int)curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
        curl_close($ch);
        if ($response === false) {
            return ['success' => false, 'message' => 'openWB Pro nicht erreichbar: ' . $err];
        }
        if ($code < 200 || $code >= 300) {
            return ['success' => false, 'message' => 'openWB Pro meldet HTTP ' . $code];
        }
        return ['success' => true, 'message' => 'Update-Befehl gesendet', 'http_code' => $code, 'raw' => substr((string)$response, 0, 160)];
    }

    $ctx = stream_context_create([
        'http' => [
            'method' => 'POST',
            'header' => "Content-Type: application/x-www-form-urlencoded\r\nContent-Length: " . strlen($body) . "\r\n",
            'content' => $body,
            'timeout' => 8,
            'ignore_errors' => true,
        ],
    ]);
    $response = @file_get_contents($url, false, $ctx);
    if ($response === false) {
        return ['success' => false, 'message' => 'openWB Pro nicht erreichbar'];
    }
    $statusLine = $http_response_header[0] ?? '';
    $code = 0;
    if (preg_match('/\s(\d{3})\s/', $statusLine, $m)) {
        $code = (int)$m[1];
    }
    if ($code > 0 && ($code < 200 || $code >= 300)) {
        return ['success' => false, 'message' => 'openWB Pro meldet HTTP ' . $code];
    }
    return ['success' => true, 'message' => 'Update-Befehl gesendet', 'http_code' => $code, 'raw' => substr((string)$response, 0, 160)];
}

function wallboxAuditOpenWbProUpdate($wbId, $success, $detail = '') {
    $entry = [
        'ts' => round(microtime(true), 3),
        'wb' => max(1, min(2, (int)$wbId)),
        'driver' => 'OpenWBProCharger',
        'mode' => null,
        'native_enabled' => true,
        'owner' => 'webui',
        'action' => 'openwb_pro_firmware_update',
        'decision' => $success ? 'allowed' : 'blocked',
        'reason' => $success ? 'user_requested_firmware_update' : 'firmware_update_failed',
        'payload' => ['update' => '1', 'detail' => substr((string)$detail, 0, 120)],
    ];
    $file = '/var/www/html/logs/wallbox_command_audit.log';
    @mkdir(dirname($file), 0775, true);
    @file_put_contents($file, json_encode($entry, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) . "\n", FILE_APPEND | LOCK_EX);
}

function wallboxReadOpenwbRuntimeData($wbId) {
    $path = ((int)$wbId === 2) ? '/var/www/html/ramdisk/openwb_data_wb2.json' : '/var/www/html/ramdisk/openwb_data.json';
    if (!is_readable($path) || time() - @filemtime($path) > 60) return [];
    $data = @json_decode((string)@file_get_contents($path), true);
    return is_array($data) ? $data : [];
}

function wallboxOpenwbCapabilityRows($cfg) {
    $rows = [];
    for ($wb = 1; $wb <= 2; $wb++) {
        $typeKey = $wb === 1 ? 'wb_native_type' : 'wb_native_type2';
        $ipKey = $wb === 1 ? 'wb_native_ip' : 'wb_native_ip2';
        $type = normalizeWallboxTypeConfig($cfg[$typeKey] ?? '');
        if (!in_array($type, ['openwb', 'openwb_pro'], true)) continue;
        $runtime = wallboxReadOpenwbRuntimeData($wb);
        $isPro = $type === 'openwb_pro';
        $canSwitch = $isPro || !empty($runtime['can_switch_phases']);
        $apiSurface = $runtime['api_surface'] ?? ($isPro ? 'openwb_pro_connect_php' : 'openwb_secondary_set_current_heartbeat');
        $source = $runtime['phase_switch_source'] ?? ($isPro ? 'openwb_pro_connect_php' : 'noch nicht erkannt');
        $capability = $runtime['phase_switch_capability'] ?? ($isPro ? 'official_connect_php' : 'unknown');
        $controlLabel = $runtime['control_label'] ?? ($isPro ? 'Direkt steuerbar' : 'openWB regelt selbst');
        $controlDetail = $runtime['control_detail'] ?? '';
        $controlLevel = $runtime['control_level'] ?? 'info';
        $configuredRole = $runtime['configured_role'] ?? '';
        $detectedRole = $runtime['detected_role'] ?? '';
        $effectiveRole = $runtime['effective_role'] ?? '';
        $roleMismatch = !empty($runtime['role_mismatch']);
        $failureCount = (int)($runtime['command_failure_count'] ?? 0);
        $failureLimit = (int)($runtime['command_failure_limit'] ?? 3);
        $commandBlocked = !empty($runtime['command_blocked']);
        $chargepointName = $runtime['chargepoint_name'] ?? '';
        $rows[] = [
            'wb' => $wb,
            'type' => $type,
            'host' => trim((string)($cfg[$ipKey] ?? '')),
            'can_switch' => $canSwitch,
            'api_surface' => $apiSurface,
            'source' => $source,
            'capability' => $capability,
            'control_label' => $controlLabel,
            'control_detail' => $controlDetail,
            'control_level' => $controlLevel,
            'configured_role' => $configuredRole,
            'detected_role' => $detectedRole,
            'effective_role' => $effectiveRole,
            'role_mismatch' => $roleMismatch,
            'command_failure_count' => $failureCount,
            'command_failure_limit' => $failureLimit,
            'command_blocked' => $commandBlocked,
            'chargepoint_name' => $chargepointName,
            'fresh' => !empty($runtime),
        ];
    }
    return $rows;
}

function wallboxE3dcCapabilityRows($cfg) {
    $runtimePath = '/var/www/html/ramdisk/wallbox_native.json';
    $runtime = [];
    if (is_readable($runtimePath) && time() - @filemtime($runtimePath) <= 60) {
        $decoded = @json_decode((string)@file_get_contents($runtimePath), true);
        if (is_array($decoded)) $runtime = $decoded;
    }
    $details = isset($runtime['wb_details']) && is_array($runtime['wb_details']) ? $runtime['wb_details'] : [];
    $detailById = [];
    foreach ($details as $detail) {
        if (!is_array($detail)) continue;
        $id = (int)($detail['id'] ?? 0);
        if ($id > 0) $detailById[$id] = $detail;
    }
    $types = [
        'native', 'e3dc', 'e3dc_legacy', 'e3dc_auto', 'e3dc_efy',
        'e3dc_easy', 'e3dc_easy_connect', 'e3dc_multi',
        'e3dc_multi_connect', 'e3dc_multi_connect_ii',
    ];
    $rows = [];
    for ($wb = 1; $wb <= 2; $wb++) {
        $typeKey = $wb === 1 ? 'wb_native_type' : 'wb_native_type2';
        $type = normalizeWallboxTypeConfig($cfg[$typeKey] ?? '');
        if (!in_array($type, $types, true)) continue;
        $detail = $detailById[$wb] ?? [];
        $rows[] = [
            'wb' => $wb,
            'configured_type' => $type,
            'family' => (string)($detail['e3dc_device_family'] ?? 'unknown'),
            'family_source' => (string)($detail['e3dc_device_family_source'] ?? 'unknown'),
            'firmware' => (string)($detail['firmware_version'] ?? ''),
            'rscp_type' => $detail['e3dc_rscp_wallbox_type'] ?? null,
            'capability' => (string)($detail['e3dc_capability_state'] ?? 'status_unavailable'),
            'readback_complete' => !empty($detail['e3dc_direct_readback_complete']),
            'backend' => (string)($detail['e3dc_control_backend'] ?? 'status_only'),
            'backend_label' => (string)($detail['e3dc_backend_label'] ?? 'Nur Status – WBchar6 nicht gewählt; Direkte Übergänge gesperrt'),
            'fresh' => !empty($detail),
        ];
    }
    return $rows;
}

function findSavedCarIndexForIdentifiers($cars, $identifiers, $name = '') {
    $probes = [];
    $typed = [
        'profile_id' => ['id', 'profile_id'],
        'car_id' => ['id', 'profile_id'],
        'vehicle_id' => ['vehicle_id', 'vehicle_mac', 'mac'],
        'rfid_tag' => ['rfid', 'rfid_tag'],
        'cloud_vehicle_id' => ['cloud_vehicle_id'],
    ];
    foreach ($identifiers as $type => $id) {
        $compact = compactVehicleIdentifierWallbox($id);
        if ($compact === '') continue;
        $probeType = is_string($type) && isset($typed[$type]) ? $type : 'legacy_any';
        if (!isset($probes[$probeType])) $probes[$probeType] = [];
        $probes[$probeType][] = $compact;
    }
    $nameNorm = normalizeVehicleNameWallbox($name);

    foreach ($cars as $idx => $car) {
        if (!is_array($car)) continue;
        foreach ($probes as $type => $values) {
            $fields = $type === 'legacy_any'
                ? ['id', 'profile_id', 'vehicle_id', 'vehicle_mac', 'mac', 'rfid', 'rfid_tag', 'cloud_vehicle_id']
                : $typed[$type];
            foreach ($fields as $key) {
                $saved = compactVehicleIdentifierWallbox($car[$key] ?? '');
                if ($saved !== '' && in_array($saved, $values, true)) return $idx;
            }
        }
    }
    if (!empty($probes)) return null;
    foreach ($cars as $idx => $car) {
        if (!is_array($car)) continue;
        $savedName = normalizeVehicleNameWallbox($car['name'] ?? '');
        if ($nameNorm !== '' && $savedName !== '' && $nameNorm === $savedName) return $idx;
    }
    return null;
}

function normalizeVehicleNameWallbox($value) {
    $value = trim((string)$value);
    $value = preg_replace('/\s+/u', ' ', $value);
    if (function_exists('mb_strtolower')) return mb_strtolower($value, 'UTF-8');
    return strtolower($value);
}

function newSavedCarProfileIdWallbox($cars) {
    $known = [];
    foreach ((array)$cars as $car) {
        if (is_array($car) && !empty($car['id'])) $known[(string)$car['id']] = true;
    }
    do {
        try {
            $suffix = bin2hex(random_bytes(8));
        } catch (Throwable $e) {
            $suffix = str_replace('.', '', uniqid('', true));
        }
        $id = 'custom_' . $suffix;
    } while (isset($known[$id]));
    return $id;
}

function getLiveCloudVehiclesForWallbox() {
    $vehiclesData = readJsonArrayWallbox('/var/www/html/ramdisk/vehicles.json', []);
    $vehicles = $vehiclesData['vehicles'] ?? $vehiclesData;
    return is_array($vehicles) ? $vehicles : [];
}

function wallboxPageSocRuleConfirmed($source, $ruleConfirmed = null) {
    if ($ruleConfirmed === true || $ruleConfirmed === 1 || $ruleConfirmed === '1' || $ruleConfirmed === 'true') return true;
    $source = strtolower(trim((string)$source));
    if ($source === '' || in_array($source, ['simple_view_start_soc', 'config_start_soc', 'configured_wallbox'], true)) return false;
    if (strpos($source, 'wallbox_estimated_from_') === 0) {
        return wallboxPageSocRuleConfirmed(substr($source, strlen('wallbox_estimated_from_')), null);
    }
    if (strpos($source, 'wallbox_estimated') === 0) return false;
    if (in_array($source, ['manual_start_soc', 'manual_soc', 'manual', 'openwb_profile_link', 'openwb_pro_raw', 'openwb_pro_estimated'], true)) return true;
    foreach (['mqtt', 'bluelink', 'wallbox', 'openwb', 'vehicle', 'car_soc', 'hyundai', 'kia'] as $token) {
        if (strpos($source, $token) !== false) return true;
    }
    return false;
}

function getDetectedOpenwbVehiclesForWallbox($savedCars) {
    $detected = [];
    $files = [
        1 => '/var/www/html/ramdisk/openwb_data.json',
        2 => '/var/www/html/ramdisk/openwb_data_wb2.json',
    ];
    foreach ($files as $wb => $path) {
        $data = readJsonArrayWallbox($path, []);
        if (empty($data)) continue;
        $plugged = wallboxTruthy($data['plug_state'] ?? false);
        $charging = wallboxTruthy($data['charge_state'] ?? false) || ((float)($data['power_w'] ?? 0) > 50);
        if (!$plugged && !$charging) continue;
        $stableIdentityCurrent = !empty($data['stable_vehicle_identity_current']);
        if (!$stableIdentityCurrent) continue;
        $vehicleId = trim((string)($data['vehicle_id'] ?? $data['rfid_tag'] ?? ''));
        $name = trim((string)($data['car_name'] ?? ''));
        if ($vehicleId === '' && $name === '') continue;
        $match = findSavedCarIndexForIdentifiers($savedCars, [
            'vehicle_id' => $data['vehicle_id'] ?? '',
            'rfid_tag' => $data['rfid_tag'] ?? '',
            'car_id' => $data['car_id'] ?? '',
        ], $name);
        if ($match !== null) continue;
        $socSource = (string)($data['car_soc_source'] ?? '');
        $socConfirmed = wallboxPageSocRuleConfirmed($socSource, $data['car_soc_rule_confirmed'] ?? null);
        $detected[] = [
            'wb' => $wb,
            'name' => $name ?: ($wb === 1 ? 'openWB Fahrzeug' : 'openWB Pro Fahrzeug'),
            'vehicle_id' => $vehicleId,
            'soc' => $socConfirmed ? ($data['car_soc'] ?? null) : null,
            'range_km' => $socConfirmed ? ($data['car_range'] ?? null) : null,
            'capacity' => $data['car_capacity_kwh'] ?? 0,
            'power' => (int)($data['phases_in_use'] ?? 0) >= 3 ? 11.0 : 7.4,
            'max_phases' => (int)($data['phases_in_use'] ?? 0) >= 3 ? 3 : 1,
            'source' => $socSource !== '' ? $socSource : ($data['source'] ?? ($wb === 1 ? 'openWB' : 'openWB Pro')),
        ];
    }
    return $detected;
}

function getObservedOpenwbChargeProfilesForWallbox() {
    $profiles = [];
    foreach ([
        1 => '/var/www/html/ramdisk/openwb_data.json',
        2 => '/var/www/html/ramdisk/openwb_data_wb2.json',
    ] as $wb => $path) {
        $data = readJsonArrayWallbox($path, []);
        $profileName = trim((string)($data['charge_template_name'] ?? ''));
        if ($profileName === '') continue;
        $stableIdentityCurrent = !empty($data['stable_vehicle_identity_current']);
        $profiles[] = [
            'wb' => $wb,
            'name' => $profileName,
            'vehicle_name' => trim((string)($data['car_name'] ?? '')),
            'vehicle_id' => $stableIdentityCurrent
                ? trim((string)($data['vehicle_id'] ?? $data['rfid_tag'] ?? ''))
                : '',
            'stable_identity_current' => $stableIdentityCurrent,
            'retained_identity_present' => !$stableIdentityCurrent
                && trim((string)($data['vehicle_id'] ?? $data['rfid_tag'] ?? $data['car_id'] ?? '')) !== '',
        ];
    }
    return $profiles;
}

function wallboxVehicleMaxPhases($vehicle) {
    if (!is_array($vehicle)) return '';
    foreach (['max_phases', 'phases', 'ac_phases', 'charge_phases', 'charging_phases'] as $key) {
        if (isset($vehicle[$key]) && is_numeric($vehicle[$key])) {
            $ph = (int)$vehicle[$key];
            if (in_array($ph, [1, 2, 3], true)) return $ph;
        }
    }
    $power = isset($vehicle['power']) ? (float)$vehicle['power'] : (float)($vehicle['charge_power'] ?? $vehicle['charge_power_kw'] ?? 0);
    if ($power > 0 && $power <= 7.6) return 1;
    if ($power >= 8.0) return 3;
    return '';
}

function addWallboxVehicleOption(&$options, $vehicle, $fallbackName = '') {
    if (!is_array($vehicle)) return;
    $id = trim((string)($vehicle['id'] ?? $vehicle['profile_id'] ?? $vehicle['vehicle_id'] ?? ''));
    if ($id === '') return;
    $name = trim((string)($vehicle['name'] ?? $vehicle['cloud_vehicle_name'] ?? $fallbackName));
    if ($name === '') $name = $id;
    $aliases = wallboxVehicleAliases($vehicle);
    foreach ($options as $existingId => $existing) {
        $existingAliases = $existing['aliases'] ?? [];
        if (!empty($aliases) && !empty(array_intersect($aliases, $existingAliases))) {
            $soc = $vehicle['soc'] ?? null;
            if ($soc !== null && $soc !== '' && is_numeric($soc) && isset($options[$existingId])) {
                $socRounded = round((float)$soc);
                $existingName = trim((string)($options[$existingId]['name'] ?? $name));
                if ($existingName === '') $existingName = $name ?: $id;
                $options[$existingId]['soc'] = $socRounded;
                $options[$existingId]['label'] = $existingName . ' (' . $socRounded . '%)';
            }
            foreach ([
                'capacity' => $vehicle['capacity'] ?? $vehicle['capacity_kwh'] ?? '',
                'power' => $vehicle['power'] ?? $vehicle['charge_power'] ?? $vehicle['charge_power_kw'] ?? '',
                'target_soc' => $vehicle['target_soc'] ?? $vehicle['targetSoc'] ?? '',
                'max_soc' => $vehicle['max_soc'] ?? $vehicle['max_soc_si'] ?? $vehicle['target_soc'] ?? '',
            ] as $field => $value) {
                if (($options[$existingId][$field] ?? '') === '' && $value !== '' && $value !== null) {
                    $options[$existingId][$field] = $value;
                }
            }
            return;
        }
    }
    if (isset($options[$id])) {
        $soc = $vehicle['soc'] ?? null;
        if ($soc !== null && $soc !== '' && is_numeric($soc)) {
            $socRounded = round((float)$soc);
            $existingName = trim((string)($options[$id]['name'] ?? $name));
            if ($existingName === '') $existingName = $name ?: $id;
            $options[$id]['soc'] = $socRounded;
            $options[$id]['label'] = $existingName . ' (' . $socRounded . '%)';
        }
        foreach ([
            'capacity' => $vehicle['capacity'] ?? $vehicle['capacity_kwh'] ?? '',
            'power' => $vehicle['power'] ?? $vehicle['charge_power'] ?? $vehicle['charge_power_kw'] ?? '',
            'target_soc' => $vehicle['target_soc'] ?? $vehicle['targetSoc'] ?? '',
            'max_soc' => $vehicle['max_soc'] ?? $vehicle['max_soc_si'] ?? $vehicle['target_soc'] ?? '',
        ] as $field => $value) {
            if (($options[$id][$field] ?? '') === '' && $value !== '' && $value !== null) {
                $options[$id][$field] = $value;
            }
        }
        return;
    }
    $soc = $vehicle['soc'] ?? null;
    $label = $name;
    if ($soc !== null && $soc !== '' && is_numeric($soc)) {
        $label .= ' (' . round((float)$soc) . '%)';
    }
    $options[$id] = [
        'id' => $id,
        'label' => $label,
        'name' => $name,
        'soc' => ($soc !== null && $soc !== '' && is_numeric($soc)) ? round((float)$soc) : '',
        'capacity' => $vehicle['capacity'] ?? $vehicle['capacity_kwh'] ?? '',
        'power' => $vehicle['power'] ?? $vehicle['charge_power'] ?? $vehicle['charge_power_kw'] ?? '',
        'max_phases' => wallboxVehicleMaxPhases($vehicle),
        'target_soc' => $vehicle['target_soc'] ?? $vehicle['targetSoc'] ?? '',
        'max_soc' => $vehicle['max_soc'] ?? $vehicle['max_soc_si'] ?? $vehicle['target_soc'] ?? '',
        'aliases' => $aliases,
    ];
}

function buildWallboxVehicleOptions($savedCars, $liveVehicles, $selectedIds = []) {
    $options = [
        '__none' => ['id' => '__none', 'label' => 'Kein Fahrzeug...', 'name' => ''],
        'none' => ['id' => 'none', 'label' => 'Gast-Fahrzeug', 'name' => 'Gast-Fahrzeug'],
    ];
    foreach (($savedCars ?? []) as $vehicle) {
        addWallboxVehicleOption($options, $vehicle);
    }
    foreach (($liveVehicles ?? []) as $vehicle) {
        addWallboxVehicleOption($options, $vehicle);
    }
    foreach ($selectedIds as $selectedId) {
        $selectedId = normalizeWallboxVehicleSelection($selectedId);
        if ($selectedId !== '__none' && $selectedId !== 'none' && !isset($options[$selectedId])) {
            $options[$selectedId] = [
                'id' => $selectedId,
                'label' => 'Gespeichert: ' . $selectedId,
                'name' => $selectedId,
            ];
        }
    }
    return $options;
}

// NEU: Konfiguration vorab laden, um Pfade zu bestimmen
$confData = loadE3dcConfig($base_path);

// Priorität 1: Der dedizierte Pfad aus der Config (wie bei ebaM)
$wallbox_file = $base_path . 'e3dc.wallbox.txt'; // Default fallback

$wb_path_config = isset($confData['config']['wallbox']) ? $confData['config']['wallbox'] : 'true';
$e3dcwallboxtxt = isset($confData['config']['e3dcwallboxtxt']) ? trim($confData['config']['e3dcwallboxtxt']) : '';

// Altlasten-Bereinigung: Falls noch Inline-Kommentare drankleben
$wb_path_clean = explode('//', $wb_path_config)[0];
$wb_path_clean = explode('#', $wb_path_clean)[0];
$wb_path_clean = trim($wb_path_clean, " \t\n\r\0\x0B\"");
$wb_path_lower = strtolower($wb_path_clean);

if (!empty($e3dcwallboxtxt)) {
    // Eba's Logik: Es ist meistens ein Verzeichnispfad, Eba hängt immer "e3dc.wallbox.txt" an
    if (substr($e3dcwallboxtxt, -1) !== '/' && strpos(substr($e3dcwallboxtxt, -4), '.txt') === false) {
        $e3dcwallboxtxt .= '/';
    }
    if (strpos(substr($e3dcwallboxtxt, -4), '.txt') === false) {
        $wallbox_file = $e3dcwallboxtxt . 'e3dc.wallbox.txt';
    } else {
        $wallbox_file = $e3dcwallboxtxt;
    }
}
// Abwärtskompatibilität: Falls jemand den Pfad in den Switch 'wallbox' geschrieben hat
elseif (!in_array($wb_path_lower, ['true', '1', 'false', '0', '-1', 'yes', 'no', ''])) {
    if (strpos($wb_path_clean, '/') !== false || strpos($wb_path_clean, '.txt') !== false) {
        $wallbox_file = $wb_path_clean;
    }
}

// AJAX-Behandlung für die Live-Wallbox-Steuerung (Sperre und Modi)
if (isset($_POST['save_wb_status_ajax'])) {
    $updates = [];
    $removeEmergency = false;
    $wbId = (int)($_POST['wb_id'] ?? 1);
    if (isset($_POST['wb_locked'])) {
        $updates["wb{$wbId}_locked"] = $_POST['wb_locked'] === '1' ? '1' : '0';
        if ($_POST['wb_locked'] === '0') {
            $removeEmergency = true;
        }
    }
    if (isset($_POST['wb_mode'])) {
        $newMode = normalizeWallboxModeValue($_POST['wb_mode']);
        $oldMode = normalizeWallboxModeValue($confData['config']["wb{$wbId}_mode"] ?? '0');
        $updates["wb{$wbId}_mode"] = $newMode;
        clearWallboxManualPauseOnModeChange($updates, $wbId, $newMode, $oldMode);
        // wb_native_mode ist ausschließlich die Multi-WB-Verteilpriorität
        // (0=ausgeglichen, 1=WB1, 2=WB2). Der Lade-Modus steht nur in wb1/2_mode.
    }
    if (isset($_POST['wb_observe_storage_policy'])) {
        $updates["wb{$wbId}_observe_storage_policy"] = normalizeWallboxObserveStoragePolicy($_POST['wb_observe_storage_policy']);
    }
    if (isset($_POST['wb_name'])) {
        $updates["wb{$wbId}_name"] = trim($_POST['wb_name']);
    }
    if (isset($_POST['wb_battery_departure_time'])) {
        $updates["wb{$wbId}_battery_departure_time"] = normalizeWallboxDepartureTime($_POST['wb_battery_departure_time']);
    }
    if (isset($_POST['wb_battery_departure_window_h'])) {
        $updates["wb{$wbId}_battery_departure_window_h"] = normalizeWallboxDepartureWindowHours($_POST['wb_battery_departure_window_h']);
    }
    $txOptions = [
        'operation' => isset($newMode) ? ($newMode === '0' ? 'clear' : 'plan') : 'preserve',
        'emergency_flag' => $removeEmergency ? 'remove' : 'preserve',
    ];
    if (isset($newMode, $oldMode)) {
        $txOptions['mode_transition'] = ['wb_id' => $wbId, 'new_mode' => $newMode, 'old_mode' => $oldMode];
    }
    $tx = e3dcWallboxPlanTransaction($updates, $txOptions);
    if (!empty($tx['success'])) {
        echo "OK";
    } else {
        header("HTTP/1.1 500 Internal Server Error");
        echo "Transaction Error: " . ($tx['code'] ?? 'unknown');
    }
    exit;
}

if (isset($_POST['save_wb_manual_pause_ajax'])) {
    $wbId = max(1, min(2, (int)($_POST['wb_id'] ?? 1)));
    $pause = wallboxTruthy($_POST['manual_pause'] ?? '0') ? '1' : '0';
    $tx = e3dcWallboxPlanTransaction(
        ["wb{$wbId}_manual_pause" => $pause],
        ['operation' => 'preserve']
    );
    if (!empty($tx['success'])) {
        $updatedConf = loadE3dcConfig($base_path);
        $updatedConfig = $updatedConf['config'] ?? [];
        $wb1Pause = wallboxTruthy($updatedConfig['wb1_manual_pause'] ?? '0');
        $wb2Pause = wallboxTruthy($updatedConfig['wb2_manual_pause'] ?? '0');
        header('Content-Type: application/json; charset=utf-8');
        echo json_encode([
            'ok' => true,
            'wb' => $wbId,
            'manual_pause' => $wbId === 2 ? $wb2Pause : $wb1Pause,
            'wb1_manual_pause' => $wb1Pause,
            'wb2_manual_pause' => $wb2Pause,
        ], JSON_UNESCAPED_UNICODE);
    } else {
        header("HTTP/1.1 500 Internal Server Error");
        echo "Schreibfehler";
    }
    exit;
}

if (isset($_POST['save_simple_wallbox_mode_ajax'])) {
    $wbId = max(1, min(2, (int)($_POST['simple_wb_id'] ?? 1)));
    [$energyMode, $chargeIntent] = normalizeWallboxSimpleChoice(
        $_POST['simple_energy_mode'] ?? 'pv',
        $_POST['simple_charge_intent'] ?? 'surplus'
    );

    if ($chargeIntent === 'scheduled') {
        echo "PLAN_REQUIRED";
        exit;
    }

    $newMode = simpleWallboxNativeMode($energyMode, $chargeIntent);
    $oldMode = normalizeWallboxModeValue($confData['config']["wb{$wbId}_mode"] ?? '0');
    $planHours = ($chargeIntent === 'instant' && $energyMode === 'grid_price') ? '99' : '0';
    $observeStoragePolicy = simpleWallboxObserveStoragePolicy($energyMode, $chargeIntent);
    $reserveSoc = sanitizeWallboxHouseReserveValue($_POST['simple_house_reserve'] ?? ($confData['config']['wbminsoc'] ?? '70'), 70);
    $priceLimit = normalizeWallboxPriceLimitValue($_POST['simple_price_limit'] ?? ($confData['config']['dvcarlimit'] ?? '0.0'), 0.0);

    $updates = [
        "wb{$wbId}_mode" => $newMode,
        "wb{$wbId}_locked" => '0',
        "wb{$wbId}_smart_wbhour_enable" => '0',
        "wb{$wbId}_plan_hours" => $planHours,
        "wb{$wbId}_observe_storage_policy" => $observeStoragePolicy,
    ];
    if (isset($_POST['simple_house_reserve'])) {
        $updates['wbminsoc'] = $reserveSoc;
    }
    if (isset($_POST['simple_price_limit'])) {
        $updates['dvcarlimit'] = $priceLimit;
    }
    clearWallboxManualPauseOnModeChange($updates, $wbId, $newMode, $oldMode);
    if ($wbId === 1) {
        $updates['smart_wbhour_enable'] = '0';
        $updates['wbhour'] = $planHours;
        $updates['wb_sofort'] = ($planHours === '99') ? '1' : '0';
    }

    $tx = e3dcWallboxPlanTransaction($updates, [
        'operation' => $planHours === '0' ? 'clear' : 'plan',
        'abort_flag' => $planHours === '0' ? 'create' : 'remove',
        'mode_transition' => ['wb_id' => $wbId, 'new_mode' => $newMode, 'old_mode' => $oldMode],
    ]);
    if (!empty($tx['success'])) {
        echo "OK";
    } else {
        header("HTTP/1.1 500 Internal Server Error");
        echo "Transaction Error: " . ($tx['code'] ?? 'unknown');
    }
    exit;
}

if (isset($_POST['save_simple_wallbox_limits_ajax'])) {
    $reserveSoc = sanitizeWallboxHouseReserveValue($_POST['simple_house_reserve'] ?? ($confData['config']['wbminsoc'] ?? '70'), 70);
    $priceLimit = normalizeWallboxPriceLimitValue($_POST['simple_price_limit'] ?? ($confData['config']['dvcarlimit'] ?? '0.0'), 0.0);
    $tx = e3dcWallboxPlanTransaction([
        'wbminsoc' => $reserveSoc,
        'dvcarlimit' => $priceLimit,
    ], ['operation' => 'preserve']);
    if (!empty($tx['success'])) {
        echo "OK";
    } else {
        header("HTTP/1.1 500 Internal Server Error");
        echo "Schreibfehler";
    }
    exit;
}

if (isset($_POST['save_simple_wallbox'])) {
    $wbId = max(1, min(2, (int)($_POST['simple_wb_id'] ?? 1)));
    $savedCarsForSelection = readJsonArrayWallbox('/var/www/html/data/saved_cars.json', []);
    [$energyMode, $chargeIntent] = normalizeWallboxSimpleChoice(
        $_POST['simple_energy_mode'] ?? 'pv',
        $_POST['simple_charge_intent'] ?? 'surplus'
    );
    $newMode = simpleWallboxNativeMode($energyMode, $chargeIntent);
    $oldMode = normalizeWallboxModeValue($confData['config']["wb{$wbId}_mode"] ?? '0');
    $readyBy = normalizeWallboxSimpleReadyTime($_POST['simple_ready_by'] ?? '07:00');
    $targetUnit = normalizeWallboxSimpleTargetUnit($_POST['simple_target_unit'] ?? 'soc');
    $targetSoc = sanitizeWallboxPercentValue($_POST['simple_target_soc'] ?? '80', 80);
    $targetKwh = sanitizeWallboxKwhValue($_POST['simple_target_kwh'] ?? '20', 20);
    $reserveSoc = sanitizeWallboxHouseReserveValue($_POST['simple_house_reserve'] ?? ($confData['config']['wbminsoc'] ?? '70'), 70);
    $priceLimit = normalizeWallboxPriceLimitValue($_POST['simple_price_limit'] ?? ($confData['config']['dvcarlimit'] ?? '0.0'), 0.0);
    $vehicleId = canonicalWallboxVehicleSelection($_POST['simple_vehicle_id'] ?? '__none', $savedCarsForSelection);
    $vehicleName = trim((string)($_POST['simple_vehicle_name'] ?? $vehicleId));
    $capacity = sanitizeWallboxKwhValue($_POST['simple_capacity'] ?? ($confData['config']["wb{$wbId}_capacity"] ?? $confData['config']['car_capacity'] ?? '72.0'), 72.0);
    $chargePower = sanitizeWallboxKwValue($_POST['simple_charge_power'] ?? ($confData['config']["wb{$wbId}_charge_power"] ?? $confData['config']['car_charge_power'] ?? '11.0'), 11.0);
    $smartEnabled = ($chargeIntent === 'scheduled' && $energyMode === 'grid_price') ? '1' : '0';
    $planHours = ($chargeIntent === 'instant' && $energyMode === 'grid_price') ? '99' : '0';
    $observeStoragePolicy = simpleWallboxObserveStoragePolicy($energyMode, $chargeIntent);

    $updates = [
        "wb{$wbId}_mode" => $newMode,
        "wb{$wbId}_locked" => '0',
        "wb{$wbId}_car_id" => $vehicleId,
        "wb{$wbId}_capacity" => $capacity,
        "wb{$wbId}_charge_power" => $chargePower,
        "wb{$wbId}_target_unit" => $targetUnit,
        "wb{$wbId}_target_soc" => $targetSoc,
        "wb{$wbId}_target_kwh" => $targetKwh,
        "wb{$wbId}_wbbis" => $readyBy,
        "wb{$wbId}_wbvon" => 'now',
        "wb{$wbId}_battery_departure_time" => $readyBy,
        "wb{$wbId}_smart_wbhour_enable" => $smartEnabled,
        "wb{$wbId}_plan_hours" => $planHours,
        "wb{$wbId}_observe_storage_policy" => $observeStoragePolicy,
    ];
    clearWallboxManualPauseOnModeChange($updates, $wbId, $newMode, $oldMode);

    if (isset($_POST['simple_house_reserve'])) {
        $updates['wbminsoc'] = $reserveSoc;
    }
    if (isset($_POST['simple_price_limit'])) {
        $updates['dvcarlimit'] = $priceLimit;
    }

    if ($wbId === 1) {
        $updates['car_capacity'] = $capacity;
        $updates['car_charge_power'] = $chargePower;
        $updates['car_target_unit'] = $targetUnit;
        $updates['car_target_soc'] = $targetSoc;
        $updates['car_target_kwh'] = $targetKwh;
        $updates['wbbis'] = $readyBy;
        $updates['wbvon'] = 'now';
        $updates['smart_wbhour_enable'] = $smartEnabled;
        $updates['wbhour'] = $planHours;
        $updates['wb_sofort'] = ($planHours === '99') ? '1' : '0';
    }

    $manualSoc = [];
    if ($targetUnit === 'soc' && trim((string)($_POST['simple_current_soc'] ?? '')) !== '') {
        $sample = buildWallboxManualSocSample($wbId, $_POST['simple_current_soc'], $vehicleId, $vehicleName, $capacity, 'simple_view_start_soc');
        if (is_array($sample)) $manualSoc[$wbId] = $sample;
    }

    $tx = e3dcWallboxPlanTransaction($updates, [
        'operation' => $planHours === '0' && $smartEnabled !== '1' ? 'clear' : 'plan',
        'abort_flag' => $planHours === '0' && $smartEnabled !== '1' ? 'create' : 'remove',
        'manual_soc' => $manualSoc,
        'mode_transition' => ['wb_id' => $wbId, 'new_mode' => $newMode, 'old_mode' => $oldMode],
    ]);
    if (!empty($tx['success'])) {
        $message = successMessage('Ladeplan gespeichert. Die Wallbox-Regelung wird neu berechnet.');
    } else {
        $message = errorMessage('Ladeplan nicht gespeichert', (string)($tx['message'] ?? 'Die transaktionale Planung ist fehlgeschlagen.'));
    }
}

// AJAX-Behandlung für die Multi-Wallbox-Priorität (0=beide, 1=WB1, 2=WB2).
if (isset($_POST['save_wb_priority_ajax'])) {
    requireWebAuth(true);
    $priorityMode = (int)($_POST['wb_native_mode'] ?? 0);
    if (!in_array($priorityMode, [0, 1, 2], true)) {
        $priorityMode = 0;
    }
    $canPrioritize = hasWallbox1Config($confData['config'] ?? []) && hasWallbox2Config($confData['config'] ?? []);
    if (!$canPrioritize) {
        $priorityMode = 0;
    }
    $tx = e3dcWallboxPlanTransaction(
        ['wb_native_mode' => (string)$priorityMode],
        ['operation' => 'preserve']
    );
    if (!empty($tx['success'])) {
        echo "OK";
    } else {
        header("HTTP/1.1 500 Internal Server Error");
        echo "Schreibfehler";
    }
    exit;
}

if (isset($_POST['openwb_pro_update_wb'])) {
    requireWebAuth(false);
    $updateWbId = max(1, min(2, (int)($_POST['openwb_pro_update_wb'] ?? 1)));
    $targets = wallboxOpenWbProUpdateTargets($confData['config'] ?? [], $updateWbId);
    $target = $targets[$updateWbId] ?? null;
    if (!$target) {
        $message = errorMessage('openWB Pro Update nicht möglich', 'Für diese Wallbox ist keine openWB Pro mit gültiger IP konfiguriert.');
    } else {
        $result = wallboxPostOpenWbProUpdate($target['host']);
        wallboxAuditOpenWbProUpdate($updateWbId, !empty($result['success']), $result['message'] ?? '');
        if (!empty($result['success'])) {
            $message = successMessage('Firmware-Update für ' . htmlspecialchars($target['label']) . ' wurde angestoßen.');
        } else {
            $message = errorMessage('openWB Pro Update fehlgeschlagen', htmlspecialchars($result['message'] ?? 'Unbekannter Fehler'));
        }
    }
}



if (isset($_POST['restart_manager'])) {
    requireWebAuth(false);
    $restartResult = e3dcRunServiceWrapperAction('restart', ['e3dc-wallbox-manager']);
    usleep(1000000); // 1 Sekunde warten, damit der neue Log-Eintrag für den Neustart ins Dashboard gerendert wird!
    $message = successMessage('✓ Python Wallbox-Manager wurde im Hintergrund neu gestartet.');
    if (
        empty($restartResult['success'])
        || !in_array('e3dc-wallbox-manager', $restartResult['changed'] ?? [], true)
    ) {
        $details = array_merge(
            $restartResult['errors'] ?? [],
            array_map(static fn($service) => $service . ': Dienst fehlt oder ist nicht geladen.', $restartResult['ignored'] ?? [])
        );
        $message = errorMessage('Dienst-Neustart fehlgeschlagen', implode("\n", $details ?: ['Kein bestätigter Neustart.']));
    }
}

// Behandlung für Ziel-SoC-Laden
if (isset($_POST['save_soc_settings'])) {
    $updates = [];
    $manualSocCandidates = [];
    $savedCarsForSelection = readJsonArrayWallbox('/var/www/html/data/saved_cars.json', []);
    if (isset($_POST['wbminsoc'])) {
        $updates['wbminsoc'] = sanitizeWallboxHouseReserveValue($_POST['wbminsoc'], 70);
    }

    // Wallbox 1 (WB1)
    if (isset($_POST['wb1_car_id'])) {
        $updates['wb1_car_id'] = canonicalWallboxVehicleSelection($_POST['wb1_car_id'], $savedCarsForSelection);
        $updates['wb1_capacity'] = (string)(float)str_replace(',', '.', $_POST['wb1_capacity'] ?? '72.0');
        $updates['wb1_target_soc'] = (string)(int)($_POST['wb1_target_soc'] ?? '80');
        $updates['wb1_max_soc_si'] = (string)(int)($_POST['wb1_max_soc_si'] ?? '90');
        $updates['wb1_charge_power'] = (string)(float)str_replace(',', '.', $_POST['wb1_charge_power'] ?? '11.0');

        // Abwärtskompatibilität: Auch die Schlüssel für ein einzelnes Fahrzeug setzen
        $updates['car_capacity'] = $updates['wb1_capacity'];
        $updates['car_target_soc'] = $updates['wb1_target_soc'];
        $updates['car_max_soc_si'] = $updates['wb1_max_soc_si'];
        $updates['car_charge_power'] = $updates['wb1_charge_power'];

        // Neu: Wenn Ist-SoC mitgeliefert ("Gastfahrzeug"), dann auch ins RAM schreiben
        if (isset($_POST['manual_soc_wb1']) && trim($_POST['manual_soc_wb1']) !== '') {
            $msoc = (float)str_replace(',', '.', $_POST['manual_soc_wb1']);
            $msoc = max(0, min(100, $msoc));
            $jd = [
                'soc' => $msoc,
                'name' => $updates['wb1_car_id'],
                'car_id' => $updates['wb1_car_id'],
                'capacity' => (float)$updates['wb1_capacity'],
                'wb' => 1,
                'source' => 'manual_start_soc',
                'plugged' => true,
                'ts' => time(),
            ];
            $manualSocCandidates[1] = $jd;
        }
    }

    // Wallbox 2 (WB2)
    if (isset($_POST['wb2_car_id'])) {
        $updates['wb2_car_id'] = canonicalWallboxVehicleSelection($_POST['wb2_car_id'], $savedCarsForSelection);
        $updates['wb2_capacity'] = (string)(float)str_replace(',', '.', $_POST['wb2_capacity'] ?? '72.0');
        $updates['wb2_target_soc'] = (string)(int)($_POST['wb2_target_soc'] ?? '80');
        $updates['wb2_max_soc_si'] = (string)(int)($_POST['wb2_max_soc_si'] ?? '90');
        $updates['wb2_charge_power'] = (string)(float)str_replace(',', '.', $_POST['wb2_charge_power'] ?? '11.0');

        if (isset($_POST['manual_soc_wb2']) && trim($_POST['manual_soc_wb2']) !== '') {
            $msoc = (float)str_replace(',', '.', $_POST['manual_soc_wb2']);
            $msoc = max(0, min(100, $msoc));
            $jd = [
                'soc' => $msoc,
                'name' => $updates['wb2_car_id'],
                'car_id' => $updates['wb2_car_id'],
                'capacity' => (float)$updates['wb2_capacity'],
                'wb' => 2,
                'source' => 'manual_start_soc',
                'plugged' => true,
                'ts' => time(),
            ];
            $manualSocCandidates[2] = $jd;
        }
    }

    // Die native Ladeplanung gilt je Wallbox. Die alten Felder wbhour/wbvon/wbbis
    // bleiben als Rückfallwerte für WB1 erhalten, damit alte Installationen weiterlaufen.
    for ($planWb = 1; $planWb <= 2; $planWb++) {
        $hoursKey = "native_plan_hours_wb{$planWb}";
        $smartKey = "smart_wbhour_enable_wb{$planWb}";
        $ecoKey   = "wb_native_eco_wb{$planWb}";
        $fromModeKey = "save_wbvon_mode_wb{$planWb}";
        $fromKey  = "save_wbvon_time_wb{$planWb}";
        $toKey    = "wbbis_time_wb{$planWb}";

        if (isset($_POST[$hoursKey])) {
            $planHours = (int)$_POST[$hoursKey];
            $planHours = max(0, min(24, $planHours));
            $updates["wb{$planWb}_plan_hours"] = (string)$planHours;
            if ($planWb === 1) {
                $updates['wbhour'] = (string)$planHours;
                $updates['wb_sofort'] = '0';
            }
        }
        if (isset($_POST[$smartKey])) {
            $updates["wb{$planWb}_smart_wbhour_enable"] = ($_POST[$smartKey] === '1') ? '1' : '0';
            if ($planWb === 1) {
                $updates['smart_wbhour_enable'] = $updates["wb{$planWb}_smart_wbhour_enable"];
            }
        }
        if (isset($_POST[$ecoKey])) {
            $updates["wb{$planWb}_native_eco"] = ($_POST[$ecoKey] === '1') ? '1' : '0';
            if ($planWb === 1) {
                $updates['wb_native_eco'] = $updates["wb{$planWb}_native_eco"];
            }
        }
        $fromNowKey = "save_wbvon_now_wb{$planWb}";
        if (isset($_POST[$fromModeKey]) || isset($_POST[$fromKey]) || isset($_POST[$fromNowKey])) {
            $fromMode = strtolower(trim((string)($_POST[$fromModeKey] ?? 'time')));
            $fromVal = isset($_POST[$fromKey]) ? trim($_POST[$fromKey]) : '';
            if ($fromMode === 'now' || (isset($_POST[$fromNowKey]) && $_POST[$fromNowKey] === '1')) {
                $fromVal = 'now';
            } else {
                $fromVal = preg_match('/^[0-2][0-9]:[0-5][0-9]$/', $fromVal) ? $fromVal : '00:00';
            }
            $updates["wb{$planWb}_wbvon"] = $fromVal;
            if ($planWb === 1) $updates['wbvon'] = $fromVal;
        }
        if (isset($_POST[$toKey])) {
            $toVal = trim($_POST[$toKey]);
            $toVal = preg_match('/^[0-2][0-9]:[0-5][0-9]$/', $toVal) ? $toVal : '07:00';
            $updates["wb{$planWb}_wbbis"] = $toVal;
            if ($planWb === 1) $updates['wbbis'] = $toVal;
        }
    }

    // Rückwärtskompatibilität für alte Formulare.
    if (isset($_POST['native_plan_hours'])) {
        $planHours = max(0, min(24, (int)$_POST['native_plan_hours']));
        $updates['wb1_plan_hours'] = (string)$planHours;
        $updates['wbhour'] = (string)$planHours;
        $updates['wb_sofort'] = '0';
    }

    if (isset($_POST['dvcarlimit'])) {
        $limit = (float)str_replace(',', '.', $_POST['dvcarlimit']);
        $updates['dvcarlimit'] = (string)max(0.0, min(200.0, $limit));
    }

    // Abbruchflag IMMER löschen – unabhängig vom Erfolg des Konfigurationsschreibzugs
    // (sonst bleibt der Plan gesperrt, wenn der Konfigurationsschreibzug kurz hängt)
    $tx = e3dcWallboxPlanTransaction($updates, [
        'operation' => 'plan',
        'abort_flag' => 'remove',
        'manual_soc' => $manualSocCandidates,
        // e3dc_v4.json ist für diesen AJAX-Pfad autoritativ. Der optionale
        // Der Altspiegel in der Installationswurzel darf den atomaren V4-Speichervorgang
        // unter dem Apache-Benutzer nicht blockieren.
        'sync_legacy_config' => false,
    ]);
    if (wallboxWantsConfigJsonResponse()) {
        wallboxEmitConfigJsonResponse($tx, 'wallbox_assignment');
    }
    if (!empty($tx['success'])) {
        $message = successMessage('Ladeplanung je Wallbox gespeichert.');
    } else {
        $message = errorMessage('Ladeplanung nicht gespeichert', (string)($tx['message'] ?? 'Die transaktionale Planung ist fehlgeschlagen.'));
    }
}

// Behandlung: Plan neu erstellen (löscht nur das Abbruchflag -> Planer erzeugt einen neuen Plan)
if (isset($_POST['recreate_plan'])) {
    $tx = e3dcWallboxPlanTransaction([], ['operation' => 'plan', 'abort_flag' => 'remove']);
    $message = !empty($tx['success'])
        ? successMessage('&#128640; Ladeplanung wieder aktiviert. Ein neuer Plan wurde validiert.')
        : errorMessage('Ladeplanung nicht aktiviert', (string)($tx['message'] ?? 'Die transaktionale Planung ist fehlgeschlagen.'));
}

// Behandlung: Sanfter Planabbruch (NUR Netzladeplan löschen, PV-Laden läuft weiter)
// Setzt wbhour=0 und löscht Plandateien. Die Wallbox-Sperre (wb_locked) wird NICHT gesetzt.
// Setzt native_schedule_aborted.flag damit der Wallbox-Planer die automatische Neu-Generierung
// pausiert, bis der Nutzer explizit neue Einstellungen speichert.
if (isset($_POST['abort_charging'])) {
    // Nur wbhour auf 0 setzen, KEINE Sperre (wb_locked bleibt wie es ist)
    $updates = [
        'wbhour' => '0',
        'wbvon' => 'now',
        'wbbis' => '00:00',
        'wb1_plan_hours' => '0',
        'wb1_wbvon' => 'now',
        'wb1_wbbis' => '00:00',
        'wb1_battery_departure_time' => '06:30',
        'wb1_battery_departure_window_h' => '3',
        'wb1_smart_wbhour_enable' => '0',
        'wb2_plan_hours' => '0',
        'wb2_wbvon' => 'now',
        'wb2_wbbis' => '00:00',
        'wb2_battery_departure_time' => '06:30',
        'wb2_battery_departure_window_h' => '3',
        'wb2_smart_wbhour_enable' => '0',
        'smart_wbhour_enable' => '0',
        'wb_sofort' => '0',
    ];
    $tx = e3dcWallboxPlanTransaction($updates, [
        'operation' => 'clear',
        'abort_flag' => 'create',
        'delete_legacy_schedule' => true,
    ]);

    // Schedule-Dateien löschen (kein e3dc.wallbox.txt - kein C++ Eingriff)
    // Alle Dateiziele wurden bereits atomar in e3dcWallboxPlanTransaction behandelt.

    // Abbruchflag setzen: Der Wallbox-Planer überspringt die Neuerzeugung, solange es existiert.
    // Das Abbruchflag ist Bestandteil derselben Transaktion.

    // Kein Manager-Neustart nötig: Er respektiert das Flag beim nächsten Zyklus (<30s)
    if (!empty($tx['success'])) {
        $message = (($tx['legacy_cleanup_status'] ?? '') === 'partial_failure')
            ? successMessage('Netz-Ladeplan kanonisch gelöscht. Hinweis: ' . (string)($tx['message'] ?? 'Die Legacy-Bereinigung blieb unvollständig.'))
            : successMessage('Netz-Ladeplan gelöscht. PV-Laden läuft weiter.');
    } else {
        $message = errorMessage('Ladeplan nicht gelöscht', (string)($tx['message'] ?? 'Die transaktionale Planung ist fehlgeschlagen.'));
    }
}

// Behandlung: NOT-AUS (physische Sperre, alle Wallboxen gesperrt, kein Laden mehr)
if (isset($_POST['abort_all_charging'])) {
    $updates = [
        'wb1_locked'          => '1',
        'wb2_locked'          => '1',
        'wbhour'              => '0',
        'wbvon'               => 'now',
        'wbbis'               => '00:00',
        'wb1_plan_hours'      => '0',
        'wb1_wbvon'           => 'now',
        'wb1_wbbis'           => '00:00',
        'wb1_battery_departure_time' => '06:30',
        'wb1_battery_departure_window_h' => '3',
        'wb2_plan_hours'      => '0',
        'wb2_wbvon'           => 'now',
        'wb2_wbbis'           => '00:00',
        'wb2_battery_departure_time' => '06:30',
        'wb2_battery_departure_window_h' => '3',
        'wb_sofort'           => '0',
        'smart_wbhour_enable' => '0',
        'wb1_smart_wbhour_enable' => '0',
        'wb2_smart_wbhour_enable' => '0',
    ];

    // 1. Physische Sperre in Config schreiben
    $tx = e3dcWallboxPlanTransaction($updates, [
        'operation' => 'clear',
        'abort_flag' => 'create',
        'emergency_flag' => 'create',
        'delete_legacy_schedule' => true,
        'delete_legacy_wallbox_command' => true,
    ]);

    // 2. Alle Zeitpläne löschen (inkl. C++ Relikt)
    // Alle Dateiziele wurden bereits atomar in e3dcWallboxPlanTransaction behandelt.

    // 3. Hartes Stoppflag: Der Manager priorisiert dieses Flag vor jeder Regelung
    // und setzt alle aktiven Ladepunkte hart auf STOP. Der Manager bleibt aktiv,
    // damit die Sperre gehalten wird; reine Autonom-Freigabe wäre für NOT-AUS
    // zu weich, weil einige Wallboxen dann selbstständig weiterladen könnten.
    // Das NOT-AUS-Flag ist der letzte öffentliche Commit-Schritt.

    // 4. Manager neustarten damit Sperre sofort greift
    // Kein Dienst-Neustart: Der laufende Manager konsumiert die Sperre im nächsten Zyklus.

    $message = errorMessage('&#128680; NOT-AUS', 'Alle Ladepläne gelöscht, Wallboxen gesperrt und Hard-Stop an den Wallbox-Manager übergeben. Zum Freigeben den jeweiligen Ladepunkt wieder einschalten.');
}

// Behandlung für manuellen SoC
if (isset($_POST['abort_all_charging'])) {
    if (!empty($tx['success'])) {
        $details = (($tx['legacy_cleanup_status'] ?? '') === 'partial_failure')
            ? 'Der kanonische Hard-Stop ist aktiv. Hinweis: ' . (string)($tx['message'] ?? 'Die Legacy-Bereinigung blieb unvollständig.')
            : 'Alle Ladepläne wurden transaktional gelöscht; der laufende Wallbox-Manager übernimmt die Sperre ohne Dienst-Neustart.';
        $message = errorMessage('NOT-AUS', $details);
    } else {
        $message = errorMessage('NOT-AUS nicht vollständig übernommen', (string)($tx['message'] ?? 'Die transaktionale Sperre ist fehlgeschlagen.'));
    }
}

if (isset($_POST['save_manual_soc'])) {
    $wbIdx = (int)($_POST['wb_index'] ?? 1);
    $manualSoc = (float)str_replace(',', '.', $_POST['manual_soc_value'] ?? '0');
    if ($manualSoc < 0) $manualSoc = 0;
    if ($manualSoc > 100) $manualSoc = 100;

    $data = [
        'soc' => $manualSoc,
        'car_id' => trim($_POST['manual_car_id'] ?? ''),
        'name' => trim($_POST['manual_car_name'] ?? ''),
        'capacity' => (float)str_replace(',', '.', $_POST['manual_car_capacity'] ?? '0'),
        'wb' => $wbIdx,
        'source' => 'manual_start_soc',
        'plugged' => true,
        'ts' => time()
    ];

    $tx = e3dcWallboxPlanTransaction([], [
        'operation' => 'plan',
        'abort_flag' => 'remove',
        'manual_soc' => [$wbIdx => $data],
    ]);
    if (!empty($tx['success'])) {
        // Abbruchflag IMMER löschen, wenn ein neuer SoC gesetzt wird
        $abort_flag = '/var/www/html/ramdisk/native_schedule_aborted.flag';
        $message = (($tx['legacy_projection_status'] ?? '') === 'partial_failure')
            ? successMessage('Manueller Start-SoC (' . round($manualSoc, 1) . '%) kanonisch gesetzt. Hinweis: ' . (string)($tx['message'] ?? 'Der Legacy-Spiegel blieb unvollständig.'))
            : successMessage('&#10003; Manueller Start-SoC (' . round($manualSoc, 1) . '%) für Wallbox ' . $wbIdx . ' erfolgreich gesetzt.');
    } else {
        $message = errorMessage('Manueller Start-SoC nicht übernommen', (string)($tx['message'] ?? 'Der kanonische manuelle SoC konnte nicht geschrieben werden.'));
    }
}

// Behandlung für Fahrzeugvorlagen
$saved_cars_file = '/var/www/html/data/saved_cars.json';
$savedCarsRaw = is_file($saved_cars_file) && !is_link($saved_cars_file)
    ? @file_get_contents($saved_cars_file)
    : false;
$savedCarsExpectedRevision = $savedCarsRaw === false ? 'absent' : hash('sha256', $savedCarsRaw);
if (isset($_POST['save_custom_car'])) {
    $cars = $savedCarsRaw === false ? [] : json_decode($savedCarsRaw, true);
    if (!is_array($cars)) $cars = [];
    $linkSavedId = trim($_POST['custom_car_link_saved_id'] ?? '');
    $cloudVehicleId = trim($_POST['custom_car_cloud_vehicle_id'] ?? '');
    $cloudVehicleName = trim($_POST['custom_car_cloud_vehicle_name'] ?? '');
    $vehicleId = trim($_POST['custom_car_vehicle_id'] ?? '');
    $targetIndex = null;
    if ($linkSavedId !== '') {
        foreach ($cars as $idx => $car) {
            if (($car['id'] ?? '') === $linkSavedId) {
                $targetIndex = $idx;
                break;
            }
        }
    }
    if ($targetIndex === null) {
        $targetIndex = findSavedCarIndexForIdentifiers($cars, [
            'vehicle_id' => $vehicleId,
            'cloud_vehicle_id' => $cloudVehicleId,
        ], trim($_POST['custom_car_name'] ?? ''));
    }
    $existingCar = ($targetIndex !== null && isset($cars[$targetIndex]) && is_array($cars[$targetIndex])) ? $cars[$targetIndex] : [];
    $carName = trim($_POST['custom_car_name'] ?? '');
    $newCar = [
        'id' => ($targetIndex !== null && !empty($existingCar['id'])) ? $existingCar['id'] : newSavedCarProfileIdWallbox($cars),
        'name' => $carName !== '' ? $carName : ($existingCar['name'] ?? 'Unbenannt'),
        'vehicle_id' => $vehicleId !== '' ? $vehicleId : ($existingCar['vehicle_id'] ?? ''),
        'cloud_vehicle_id' => $cloudVehicleId !== '' ? $cloudVehicleId : ($existingCar['cloud_vehicle_id'] ?? ''),
        'cloud_vehicle_name' => $cloudVehicleName !== '' ? $cloudVehicleName : ($existingCar['cloud_vehicle_name'] ?? ''),
        'capacity' => (float)str_replace(',', '.', $_POST['custom_car_capacity'] ?? '72.0'),
        'power' => (float)str_replace(',', '.', $_POST['custom_car_power'] ?? '11.0'),
        'max_phases' => max(1, min(3, (int)($_POST['custom_car_max_phases'] ?? wallboxVehicleMaxPhases([
            'power' => (float)str_replace(',', '.', $_POST['custom_car_power'] ?? '11.0')
        ])))),
        'efficiency' => (float)str_replace(',', '.', $_POST['custom_car_efficiency'] ?? '90'),
        'consumption' => (float)str_replace(',', '.', $_POST['custom_car_consumption'] ?? '18'),
        'target_soc' => (int)($_POST['custom_car_target'] ?? 80),
        'max_soc' => (int)($_POST['custom_car_max'] ?? 90)
    ];
    if ($targetIndex !== null) {
        $cars[$targetIndex] = array_merge($existingCar, $newCar);
    } else {
        $cars[] = $newCar;
    }
    $assignWb = (int)($_POST['custom_car_assign_wb'] ?? 0);
    $assignUpdates = [];
    $manualSocCandidates = [];
    $assignMessage = '';
    if (in_array($assignWb, [1, 2], true)) {
        $assignUpdates = [
                "wb{$assignWb}_car_id" => normalizeWallboxVehicleSelection($newCar['id']),
                "wb{$assignWb}_capacity" => (string)$newCar['capacity'],
                "wb{$assignWb}_target_soc" => (string)$newCar['target_soc'],
                "wb{$assignWb}_max_soc_si" => (string)$newCar['max_soc'],
                "wb{$assignWb}_charge_power" => (string)$newCar['power'],
            ];
        if ($assignWb === 1) {
            $assignUpdates['car_capacity'] = (string)$newCar['capacity'];
            $assignUpdates['car_target_soc'] = (string)$newCar['target_soc'];
            $assignUpdates['car_max_soc_si'] = (string)$newCar['max_soc'];
            $assignUpdates['car_charge_power'] = (string)$newCar['power'];
        }
        $assignMessage = " und Wallbox {$assignWb} zugeordnet";
        $detectedSoc = trim($_POST['custom_car_current_soc'] ?? '');
        if ($detectedSoc !== '') {
            $soc = max(0, min(100, (float)str_replace(',', '.', $detectedSoc)));
            $manualSocCandidates[$assignWb] = [
                    'soc' => $soc,
                    'name' => $newCar['id'],
                    'capacity' => (float)$newCar['capacity'],
                    'vehicle_id' => $newCar['vehicle_id'],
                    'car_id' => $newCar['id'],
                    'wb' => $assignWb,
                    'source' => 'openwb_profile_link',
                    'ts' => time(),
            ];
        }
    }
    $txOptions = [
        'operation' => 'plan',
        'saved_cars' => $cars,
        'expected_saved_cars_sha256' => $savedCarsExpectedRevision,
    ];
    if (!empty($manualSocCandidates)) $txOptions['manual_soc'] = $manualSocCandidates;
    $tx = e3dcWallboxPlanTransaction($assignUpdates, $txOptions);
    if (!empty($tx['success']) && !empty($tx['canonical_committed'])) {
        foreach ($assignUpdates as $key => $value) $confData['config'][$key] = $value;
        $legacyHint = (($tx['legacy_projection_status'] ?? '') === 'partial_failure')
            ? ' Hinweis: ' . (string)($tx['message'] ?? 'Der Legacy-SoC-Spiegel blieb unvollständig.')
            : '';
        $message = successMessage("✓ Fahrzeugprofil '{$newCar['name']}' gespeichert{$assignMessage}.{$legacyHint}");
    } else {
        $message = errorMessage('Fahrzeugprofil nicht übernommen', (string)($tx['message'] ?? 'Der kanonische Profil-Commit ist fehlgeschlagen.'));
    }
}
if (isset($_POST['delete_custom_car'])) {
    $cars = $savedCarsRaw === false ? [] : json_decode($savedCarsRaw, true);
    if (!is_array($cars)) $cars = [];
    $delId = $_POST['delete_custom_car'];
    $deletedCars = array_values(array_filter($cars, function($c) use ($delId) {
        return isset($c['id']) && $c['id'] === $delId;
    }));
    if (empty($deletedCars)) {
        $message = errorMessage('Fahrzeugprofil nicht gelöscht', 'Das ausgewählte Profil wurde nicht gefunden; es wurde nichts verändert.');
    } else {
        $cars = array_values(array_filter($cars, function($c) use ($delId) { return isset($c['id']) && $c['id'] !== $delId; }));
        $assignmentUpdates = [];
        foreach ([1, 2] as $slot) {
            $key = "wb{$slot}_car_id";
            $selection = $confData['config'][$key] ?? '';
            $matchesDeleted = normalizeWallboxVehicleSelection($selection) === normalizeWallboxVehicleSelection($delId);
            foreach ($deletedCars as $deletedCar) {
                if (wallboxSavedCarMatchesSelection($deletedCar, $selection)) {
                    $matchesDeleted = true;
                    break;
                }
            }
            if ($matchesDeleted) {
                $assignmentUpdates[$key] = '__none';
            }
        }
        $tx = e3dcWallboxPlanTransaction($assignmentUpdates, [
            'operation' => 'plan',
            'saved_cars' => $cars,
            'expected_saved_cars_sha256' => $savedCarsExpectedRevision,
        ]);
        if (!empty($tx['success']) && !empty($tx['canonical_committed'])) {
            foreach ($assignmentUpdates as $key => $value) {
                $confData['config'][$key] = $value;
            }
            $message = successMessage("✓ Fahrzeugprofil gelöscht" . (!empty($assignmentUpdates) ? " und Wallbox-Zuordnung zurückgesetzt." : "."));
        } else {
            $message = errorMessage('Fahrzeugprofil nicht gelöscht', (string)($tx['message'] ?? 'Der kanonische Profil-Commit ist fehlgeschlagen.'));
        }
    }
}

// Behandlung für die Cloud-Integration (Bluelink)
if (isset($_POST['save_cloud_integration'])) {
    $updates = [
        'bluelink_vin' => trim($_POST['bluelink_vin'] ?? ''),
        'bluelink_refresh_token' => trim($_POST['bluelink_refresh_token'] ?? ''),
        'bluelink_car_name' => trim($_POST['bluelink_car_name'] ?? ''),
        'bluelink_interval' => (string)(int)($_POST['bluelink_interval'] ?? '15'),
        'bluelink_ignore_plug_status' => isset($_POST['bluelink_ignore_plug_status']) && $_POST['bluelink_ignore_plug_status'] == '1' ? '1' : '0'
    ];
    if (upsertWallboxConfigValues($config_file, $updates)) {
        $message = successMessage('✓ Cloud-Integration gespeichert.');
        e3dcRunServiceWrapperAction('restart', ['e3dc-bluelink']);
    } else {
        $message = errorMessage('Schreibberechtigung fehlt', 'Konnte Cloud-Integration in der V4-Konfiguration nicht speichern.');
    }
}

$saved_cars = file_exists($saved_cars_file) ? json_decode(file_get_contents($saved_cars_file), true) : [];
if (!is_array($saved_cars)) $saved_cars = [];
$live_cloud_vehicles = getLiveCloudVehiclesForWallbox();
$unknown_openwb_vehicles = getDetectedOpenwbVehiclesForWallbox($saved_cars);
$observed_openwb_profiles = getObservedOpenwbChargeProfilesForWallbox();

function applyAwattarPriceLogic($priceRaw, $awmwst, $awnebenkosten)
{
    $multiplier = ($awmwst / 100.0) + 1.0;

    // Der Preis in e3dc.wallbox.out ist der Börsenpreis in €/MWh.
    // Für den Bruttopreis in ct/kWh muss der Wert zunächst durch 10 geteilt werden.
    return (($priceRaw / 10.0) * $multiplier) + $awnebenkosten;
}

function getTieredPrices($base_path) {
    $prices = array_fill(0, 24, null);
    $file = $base_path . 'e3dc.strompreise.txt';
    if (!file_exists($file)) return null;
    $lines = file($file, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    if (!$lines) return null;

    $parsed = [];
    foreach ($lines as $line) {
        $parts = preg_split('/\s+/', trim($line));
        if (count($parts) >= 2) {
            $parsed[(int)$parts[0]] = (float)$parts[1];
        }
    }
    ksort($parsed);

    $last_price = reset($parsed);
    for ($h = 0; $h < 24; $h++) {
        if (isset($parsed[$h])) {
            $last_price = $parsed[$h];
        }
        $prices[$h] = $last_price;
    }
    return $prices;
}

// $wallbox_file wurde bereits oben aus der Config ermittelt

$alleZeilen = [];
$zeile = '1';

function parseWallboxConfigValues($filePath) {
    $result = [
        'wbhour' => '1',
        'wbvon' => '00:00',
        'wbbis' => '23:59',
        'smart_wbhour_enable' => '0',
        'wb1_plan_hours' => '',
        'wb1_wbvon' => '',
        'wb1_wbbis' => '',
        'wb1_battery_departure_time' => '06:30',
        'wb1_battery_departure_window_h' => '3',
        'wb1_smart_wbhour_enable' => '',
        'wb1_native_eco' => '',
        'dvcarlimit' => '0.0',
        'wbminsoc' => '70',
        'wb2_plan_hours' => '0',
        'wb2_wbvon' => 'now',
        'wb2_wbbis' => '07:00',
        'wb2_battery_departure_time' => '06:30',
        'wb2_battery_departure_window_h' => '3',
        'wb2_smart_wbhour_enable' => '0',
        'wb2_native_eco' => '0',
        'car_capacity' => '72.0',
        'car_target_unit' => 'soc',
        'car_target_kwh' => '20',
        'car_target_soc' => '80',
        'car_max_soc_si' => '90',
        'car_charge_power' => '11.0',
        'wbmaxladestrom' => '16',
        'wb1_car_id' => '__none',
        'wb1_capacity' => '',
        'wb1_target_unit' => '',
        'wb1_target_kwh' => '',
        'wb1_target_soc' => '',
        'wb1_max_soc_si' => '',
        'wb1_charge_power' => '',
        'wb1_max_amp' => '',
        'wb2_car_id' => '__none',
        'wb2_capacity' => '',
        'wb2_target_unit' => 'soc',
        'wb2_target_kwh' => '20',
        'wb2_target_soc' => '',
        'wb2_max_soc_si' => '',
        'wb2_charge_power' => '',
        'wb2_max_amp' => '',
        'wb1_locked' => '0',
        'wb1_mode' => '0',
        'wb1_observe_storage_policy' => 'curve',
        'wb1_name' => '',
        'wb2_locked' => '0',
        'wb2_mode' => '0',
        'wb2_observe_storage_policy' => 'curve',
        'wb2_name' => '',
        'wb_native_enable' => '0',
        'wb_native_mode' => '0',
        'wb_no_time_limit' => '0',
        'wb_sofort' => '0',
        'wb_native_eco' => '0',
        'bluelink_refresh_token' => '',
        'bluelink_vin' => '',
        'bluelink_car_name' => '',
        'bluelink_interval' => '15',
        'bluelink_ignore_plug_status' => '0'
    ];

    if (is_file($filePath) && is_readable($filePath)) {
        $lines = file($filePath, FILE_IGNORE_NEW_LINES);
        foreach ($lines as $line) {
            if (preg_match('/^\s*([a-z0-9_]+)\s*=\s*(.*?)\s*$/i', $line, $m)) {
                $key = strtolower(trim($m[1]));
                $value = trim(trim($m[2]), '"'); // Entfernt auch Anführungszeichen
                if (array_key_exists($key, $result)) {
                    $result[$key] = $value;
                }
            }
        }
    }

    if (file_exists('/var/www/html/data/e3dc_v4.json')) {
        $jsonContent = file_get_contents('/var/www/html/data/e3dc_v4.json');
        $jsonData = json_decode($jsonContent, true);
        if (is_array($jsonData)) {
            foreach ($jsonData as $jKey => $jVal) {
                $lowerKey = strtolower(trim($jKey));
                if (array_key_exists($lowerKey, $result)) {
                    $result[$lowerKey] = $jVal;
                }
            }
        }
    }

    // Initialwerte für Wallbox 1 (Abwärtskompatibilität)
    if (empty($result['wb1_car_id'])) $result['wb1_car_id'] = '__none';
    if (empty($result['wb1_capacity']) && !empty($result['car_capacity'])) $result['wb1_capacity'] = $result['car_capacity'];
    if (empty($result['wb1_target_unit']) && !empty($result['car_target_unit'])) $result['wb1_target_unit'] = $result['car_target_unit'];
    if (empty($result['wb1_target_kwh']) && !empty($result['car_target_kwh'])) $result['wb1_target_kwh'] = $result['car_target_kwh'];
    if (empty($result['wb1_target_soc']) && !empty($result['car_target_soc'])) $result['wb1_target_soc'] = $result['car_target_soc'];
    if (empty($result['wb1_max_soc_si']) && !empty($result['car_max_soc_si'])) $result['wb1_max_soc_si'] = $result['car_max_soc_si'];
    if (empty($result['wb1_charge_power']) && !empty($result['car_charge_power'])) $result['wb1_charge_power'] = $result['car_charge_power'];
    if (empty($result['wb1_target_unit'])) $result['wb1_target_unit'] = 'soc';
    if (empty($result['wb1_target_kwh'])) $result['wb1_target_kwh'] = '20';
    if ($result['wb1_plan_hours'] === '') $result['wb1_plan_hours'] = $result['wbhour'];
    if ($result['wb1_wbvon'] === '') $result['wb1_wbvon'] = $result['wbvon'];
    if ($result['wb1_wbbis'] === '') $result['wb1_wbbis'] = $result['wbbis'];
    if ($result['wb1_smart_wbhour_enable'] === '') $result['wb1_smart_wbhour_enable'] = $result['smart_wbhour_enable'];
    if ($result['wb1_native_eco'] === '') $result['wb1_native_eco'] = $result['wb_native_eco'];
    if (empty($result['wb1_max_amp'])) $result['wb1_max_amp'] = $result['wbmaxladestrom'];

    // Wallbox 2 defaults
    if (empty($result['wb2_capacity'])) $result['wb2_capacity'] = '72.0';
    if (empty($result['wb2_target_unit'])) $result['wb2_target_unit'] = 'soc';
    if (empty($result['wb2_target_kwh'])) $result['wb2_target_kwh'] = '20';
    if (empty($result['wb2_target_soc'])) $result['wb2_target_soc'] = '80';
    if (empty($result['wb2_max_soc_si'])) $result['wb2_max_soc_si'] = '90';
    if (empty($result['wb2_charge_power'])) $result['wb2_charge_power'] = '11.0';
    if (empty($result['wb2_max_amp'])) $result['wb2_max_amp'] = $result['wbmaxladestrom'];

    // NEU: Werte aus V4 JSON überschreiben die TXT-Werte!
    $v4_path = '/var/www/html/data/e3dc_v4.json';
    if (file_exists($v4_path)) {
        $v4_data = @json_decode(@file_get_contents($v4_path), true);
        if (is_array($v4_data)) {
            foreach ($result as $k => $v) {
                if (isset($v4_data[$k])) {
                    $result[$k] = (string)$v4_data[$k];
                } elseif (isset($v4_data['config'][$k])) {
                    $result[$k] = (string)$v4_data['config'][$k];
                }
            }
        }
    }
    if (empty($result['wbmaxladestrom'])) $result['wbmaxladestrom'] = '16';
    if (empty($result['wb1_max_amp'])) $result['wb1_max_amp'] = $result['wbmaxladestrom'];
    if (empty($result['wb2_max_amp'])) $result['wb2_max_amp'] = $result['wbmaxladestrom'];

    return $result;
}

function wallboxConfigUpsertResult($success, $code) {
    return ['success' => (bool)$success, 'code' => (string)$code];
}

function wallboxLogConfigFailure($operation, $code) {
    $operation = preg_replace('/[^a-z0-9_\-]/i', '', (string)$operation) ?: 'unknown';
    $code = preg_replace('/[^a-z0-9_\-]/i', '', (string)$code) ?: 'unknown';
    error_log('[E3DC_CONFIG] operation=' . $operation . ' code=' . $code);
}

function upsertWallboxConfigValuesDetailed($filePath, $updates, $options = []) {
    $normalizedUpdates = [];
    foreach (($updates ?? []) as $key => $value) {
        $key = strtolower(trim((string)$key));
        if ($key === '' || !preg_match('/^[a-z0-9_]+$/i', $key)) {
            continue;
        }
        $normalizedUpdates[$key] = is_string($value) ? trim($value) : $value;
    }
    $updates = $normalizedUpdates;
    if (empty($updates)) {
        return wallboxConfigUpsertResult(true, 'no_changes');
    }

    $testMode = PHP_SAPI === 'cli' && !empty($options['test_mode']);
    $v4Path = $testMode ? (string)($options['v4_path'] ?? '') : '/var/www/html/data/e3dc_v4.json';
    $cachePath = $testMode ? (string)($options['cache_path'] ?? '') : '/var/www/html/ramdisk/e3dc_config_cache.json';
    $failOperation = $testMode ? (string)($options['fail_operation'] ?? '') : '';
    if ($v4Path === '' || !is_file($v4Path) || is_link($v4Path)) {
        $result = wallboxConfigUpsertResult(false, 'config_missing');
        wallboxLogConfigFailure('wallbox_config_upsert', $result['code']);
        return $result;
    }

    $lockDir = dirname($v4Path) . '/.wallbox_plan_jobs';
    if (!is_dir($lockDir)) {
        $oldUmask = umask(0077);
        $made = @mkdir($lockDir, 0700, false);
        umask($oldUmask);
        if (!$made && !is_dir($lockDir)) {
            $result = wallboxConfigUpsertResult(false, 'lock_dir_failed');
            wallboxLogConfigFailure('wallbox_config_upsert', $result['code']);
            return $result;
        }
    }
    if (is_link($lockDir) || !@chmod($lockDir, 0700)) {
        $result = wallboxConfigUpsertResult(false, 'lock_dir_failed');
        wallboxLogConfigFailure('wallbox_config_upsert', $result['code']);
        return $result;
    }
    $lockPath = $lockDir . '/.transaction.lock';
    if ($failOperation === 'lock_open') {
        $result = wallboxConfigUpsertResult(false, 'lock_open_failed');
        wallboxLogConfigFailure('wallbox_config_upsert', $result['code']);
        return $result;
    }
    $lock = @fopen($lockPath, 'c+b');
    if ($lock === false) {
        $result = wallboxConfigUpsertResult(false, 'lock_open_failed');
        wallboxLogConfigFailure('wallbox_config_upsert', $result['code']);
        return $result;
    }
    @chmod($lockPath, 0660);
    $locked = $failOperation !== 'lock' && @flock($lock, LOCK_EX);
    if (!$locked) {
        @fclose($lock);
        $result = wallboxConfigUpsertResult(false, 'lock_failed');
        wallboxLogConfigFailure('wallbox_config_upsert', $result['code']);
        return $result;
    }

    $tmpPath = '';
    $finish = function($success, $code) use (&$lock, &$tmpPath) {
        if ($tmpPath !== '' && file_exists($tmpPath)) @unlink($tmpPath);
        @flock($lock, LOCK_UN);
        @fclose($lock);
        $result = wallboxConfigUpsertResult($success, $code);
        if (!$success) wallboxLogConfigFailure('wallbox_config_upsert', $code);
        return $result;
    };

    if ($failOperation === 'read') return $finish(false, 'read_failed');
    $raw = @file_get_contents($v4Path);
    if ($raw === false) return $finish(false, 'read_failed');
    $data = json_decode($raw, true);
    if (!is_array($data)) return $finish(false, 'json_invalid');
    foreach ($updates as $key => $value) $data[$key] = $value;
    $json = json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    if ($json === false) return $finish(false, 'encode_failed');
    $payload = $json . "\n";

    if ($failOperation === 'temp') return $finish(false, 'temp_create_failed');
    try {
        $tmpPath = dirname($v4Path) . '/.e3dc-v4-' . bin2hex(random_bytes(12)) . '.tmp';
    } catch (Throwable $e) {
        return $finish(false, 'temp_create_failed');
    }
    $tmp = @fopen($tmpPath, 'x+b');
    if ($tmp === false) return $finish(false, 'temp_create_failed');
    $ok = @chmod($tmpPath, e3dcJsonAtomicFileMode($v4Path, $json));
    $written = 0;
    $length = strlen($payload);
    while ($ok && $written < $length) {
        if ($failOperation === 'write') {
            $ok = false;
            break;
        }
        $count = @fwrite($tmp, substr($payload, $written));
        if ($count === false || $count <= 0) {
            $ok = false;
            break;
        }
        $written += $count;
    }
    if (!$ok || $written !== $length) {
        @fclose($tmp);
        return $finish(false, 'write_failed');
    }
    if (!@fflush($tmp)) {
        @fclose($tmp);
        return $finish(false, 'flush_failed');
    }
    if ($failOperation === 'fsync' || (function_exists('fsync') && !@fsync($tmp))) {
        @fclose($tmp);
        return $finish(false, 'fsync_failed');
    }
    @fclose($tmp);
    $verify = @file_get_contents($tmpPath);
    if ($verify === false || !hash_equals(hash('sha256', $payload), hash('sha256', $verify))) {
        return $finish(false, 'verify_failed');
    }
    if ($failOperation === 'rename' || !@rename($tmpPath, $v4Path)) {
        return $finish(false, 'rename_failed');
    }
    $tmpPath = '';
    @chmod($v4Path, e3dcJsonAtomicFileMode($v4Path, $json));
    if ($cachePath !== '' && is_file($cachePath)) @unlink($cachePath);
    return $finish(true, 'ok');
}

function upsertWallboxConfigValues($filePath, $updates) {
    $result = upsertWallboxConfigValuesDetailed($filePath, $updates);
    return !empty($result['success']);
}

function parseTimeToMinutes($value) {
    $value = trim((string)$value);
    if (!preg_match('/^(\d{1,2})(?::(\d{1,2}))?$/', $value, $m)) {
        return false;
    }

    $hour = (int)$m[1];
    $minute = isset($m[2]) ? (int)$m[2] : 0;

    if ($hour < 0 || $hour > 23 || $minute < 0 || $minute > 59) {
        return false;
    }

    return $hour * 60 + $minute;
}

function normalizeTime($value) {
    $minutes = parseTimeToMinutes($value);
    if ($minutes === false) {
        return false;
    }

    $hour = floor($minutes / 60);
    $minute = $minutes % 60;
    return sprintf('%02d:%02d', $hour, $minute);
}

function normalizeFullHourTime($value) {
    $normalized = normalizeTime($value);
    if ($normalized === false) {
        return false;
    }

    if (substr($normalized, -2) !== '00') {
        return false;
    }

    return $normalized;
}

function normalizeHourInput($value) {
    $value = trim((string)$value);
    if ($value === '') {
        return false;
    }

    if (preg_match('/^\d{1,2}$/', $value)) {
        $hour = (int)$value;
        if ($hour < 0 || $hour > 23) {
            return false;
        }
        return sprintf('%02d:00', $hour);
    }

    return normalizeFullHourTime($value);
}

function wallboxFileHasAutomaticEntries($filePath) {
    if (!is_file($filePath) || !is_readable($filePath)) {
        return false;
    }

    $lines = file($filePath, FILE_IGNORE_NEW_LINES);
    if (!is_array($lines)) {
        return false;
    }

    foreach ($lines as $line) {
        if (preg_match('/automatik/i', (string)$line)) {
            return true;
        }
    }

    return false;
}

function wallboxFileHasAutomaticEntriesFromPreviousDay($filePath) {
    if (!is_file($filePath) || !is_readable($filePath)) {
        return false;
    }

    $lines = file($filePath, FILE_IGNORE_NEW_LINES);
    if (!is_array($lines)) {
        return false;
    }

    $inAutomaticBlock = false;
    $todayLabel = date('j.n.');

    foreach ($lines as $lineRaw) {
        $line = trim((string)$lineRaw);
        if ($line === '') {
            continue;
        }

        if (preg_match('/automatik/i', $line)) {
            $inAutomaticBlock = true;
            continue;
        }

        if ($inAutomaticBlock && preg_match('/^am\s+(\d{1,2})\.(\d{1,2})\.?/iu', $line, $m)) {
            $label = ((int)$m[1]) . '.' . ((int)$m[2]) . '.';
            if ($label !== $todayLabel) {
                return true;
            }
        }
    }

    return false;
}

$wallboxConfig = parseWallboxConfigValues($config_file);

// $confData wurde bereits oben geladen
$awmwst = isset($confData['config']['awmwst']) ? parseConfigFloat($confData['config']['awmwst']) : 19.0;
$awnebenkosten = isset($confData['config']['awnebenkosten']) ? parseConfigFloat($confData['config']['awnebenkosten']) : 0.0;
$hasWb1 = hasWallbox1Config($confData['config'] ?? []);
$hasWb2 = hasWallbox2Config($confData['config'] ?? []);
$hasAnyWb = $hasWb1 || $hasWb2;
$hasDualWb = $hasWb1 && $hasWb2;
$wbPriorityMode = (int)($wallboxConfig['wb_native_mode'] ?? '0');
if (!in_array($wbPriorityMode, [0, 1, 2], true) || !$hasDualWb) {
    $wbPriorityMode = 0;
}

// Wallbox Namen / Typen ermitteln (Format: "openWB (WB 1)")
$wb1_type_raw = isset($confData['config']['wb_native_type']) && !empty($confData['config']['wb_native_type']) ? normalizeWallboxTypeConfig($confData['config']['wb_native_type']) : 'e3dc';
$wb2_type_raw = isset($confData['config']['wb_native_type2']) && !empty($confData['config']['wb_native_type2']) ? normalizeWallboxTypeConfig($confData['config']['wb_native_type2']) : 'wb2';
// Spezielle Schreibweisen für bekannte Typen
$wb_type_labels = [
    'openwb' => 'openWB',
    'openwb_pro' => 'openWB Pro (connect.php)',
    'goe' => 'go-e',
    'e3dc' => 'E3DC Easy/Legacy',
    'e3dc_easy' => 'E3DC Easy/Legacy',
    'e3dc_easy_connect' => 'E3DC Easy Connect',
    'e3dc_efy' => 'E3DC Wallbox efy',
    'e3dc_auto' => 'E3DC Auto (efy / easy connect / multi connect)',
    'e3dc_multi' => 'E3DC Multi Connect',
    'e3dc_multi_connect' => 'E3DC Multi Connect',
    'e3dc_multi_connect_ii' => 'E3DC Multi Connect II',
    'shelly' => 'Shelly',
    'tibber' => 'Tibber Pulse',
    'fronius' => 'Fronius'
];
$wb1_type = $wb_type_labels[strtolower($wb1_type_raw)] ?? ucfirst($wb1_type_raw);
$wb2_type = $wb_type_labels[strtolower($wb2_type_raw)] ?? ucfirst($wb2_type_raw);
if (!function_exists('isE3dcNativeWallboxType')) {
    function isE3dcNativeWallboxType($typeRaw) {
        $type = strtolower(trim((string)$typeRaw));
        return in_array($type, [
            '',
            'native',
            'e3dc',
            'e3dc_easy',
            'e3dc_easy_connect',
            'e3dc_legacy',
            'e3dc_auto',
            'e3dc_efy',
            'e3dc_multi',
            'e3dc_multi_connect',
            'e3dc_multi_connect_ii',
        ], true);
    }
}

function wallboxControlMasterInfo($typeRaw, $modeValue, $lockedValue = '0') {
    $type = normalizeWallboxTypeConfig($typeRaw);
    $mode = normalizeWallboxModeValue($modeValue);
    $localAllowed = ($mode === '0') || wallboxTruthy($lockedValue);
    if ($localAllowed) {
        if ($type === 'openwb') {
            return [
                'label' => 'Master: openWB',
                'class' => 'success',
                'local' => true,
                'title' => 'E3DC-Control sendet keine Ladebefehle; openWB bzw. lokale Bedienung führt.',
            ];
        }
        if (isE3dcNativeWallboxType($type)) {
            return [
                'label' => 'Master: E3DC/lokal',
                'class' => 'secondary',
                'local' => true,
                'title' => 'E3DC-Control sendet keine Ladebefehle; E3DC oder lokale Bedienung führt.',
            ];
        }
        return [
            'label' => 'Master: lokal',
            'class' => 'secondary',
            'local' => true,
            'title' => 'E3DC-Control sendet keine Ladebefehle; die Wallbox oder ein Fremdsystem führt.',
        ];
    }
    if ($type === 'openwb') {
        return [
            'label' => 'Master: openWB',
            'class' => 'success',
            'local' => false,
            'title' => 'openWB führt Ladepunkt, PV-/Zielmodus und Phasen; E3DC-Control gibt Sollstrom und Heartbeat.',
        ];
    }
    return [
        'label' => 'Master: E3DC-Control',
        'class' => 'info',
        'local' => false,
        'title' => 'E3DC-Control führt diesen Ladepunkt; lokale manuelle Vorgaben können überschrieben werden.',
    ];
}

$wb1_default_name = "$wb1_type (WB 1)";
$wb2_default_name = "$wb2_type (WB 2)";
$wb1_name = isset($wallboxConfig['wb1_name']) && !empty($wallboxConfig['wb1_name']) ? $wallboxConfig['wb1_name'] : $wb1_default_name;
$wb2_name = isset($wallboxConfig['wb2_name']) && !empty($wallboxConfig['wb2_name']) ? $wallboxConfig['wb2_name'] : $wb2_default_name;
$wb1m = normalizeWallboxModeValue($wallboxConfig['wb1_mode'] ?? '0');
$wb2m = normalizeWallboxModeValue($wallboxConfig['wb2_mode'] ?? '0');
$wb1ObserveStoragePolicy = normalizeWallboxObserveStoragePolicy($wallboxConfig['wb1_observe_storage_policy'] ?? 'curve');
$wb2ObserveStoragePolicy = normalizeWallboxObserveStoragePolicy($wallboxConfig['wb2_observe_storage_policy'] ?? 'curve');
$wb1ManualPause = wallboxTruthy($wallboxConfig['wb1_manual_pause'] ?? '0');
$wb2ManualPause = wallboxTruthy($wallboxConfig['wb2_manual_pause'] ?? '0');
$wb1MasterInfo = wallboxControlMasterInfo($wb1_type_raw, $wb1m, $wallboxConfig['wb1_locked'] ?? '0');
$wb2MasterInfo = wallboxControlMasterInfo($wb2_type_raw, $wb2m, $wallboxConfig['wb2_locked'] ?? '0');
$resolveWallboxMaxAmp = function($key) use ($wallboxConfig) {
    $fallback = $wallboxConfig['wbmaxladestrom'] ?? '16';
    $raw = trim((string)($wallboxConfig[$key] ?? ''));
    if ($raw === '') $raw = $fallback;
    $amp = (int)round((float)str_replace(',', '.', $raw));
    return max(6, min(32, $amp));
};
$wb1MaxAmpLabel = $resolveWallboxMaxAmp('wb1_max_amp');
$wb2MaxAmpLabel = $resolveWallboxMaxAmp('wb2_max_amp');

// Ladeplanung auslesen (e3dc.wallbox.out)
$plannedEntries = [];
$wallbox_out_file = $base_path . 'e3dc.wallbox.out';
$nativePlanHashFile = '/var/www/html/ramdisk/native_wallbox_schedule.json';
$currentPlanHash = file_exists($nativePlanHashFile) ? md5_file($nativePlanHashFile) : ((file_exists($wallbox_out_file)) ? md5_file($wallbox_out_file) : '');
$activePlanSlotsBeforePost = [
    1 => wallboxCurrentPlanSlot(1),
    2 => wallboxCurrentPlanSlot(2),
];

// Ladeleistungen für die Kostenvorschau aus der Konfiguration laden
$powerOptionsStr = '7.2, 11.0, 22.0';
if (file_exists($config_file)) {
    $cLines = file($config_file, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    foreach ($cLines as $cLine) {
        if (preg_match('/^\s*wbcostpowers\s*=\s*(.*)/i', $cLine, $m)) {
            $powerOptionsStr = trim($m[1]);
            break;
        }
    }
}
$powerOptionsArr = array_map('trim', explode(',', $powerOptionsStr));

$powerOptions = [];
foreach ($powerOptionsArr as $p_str) {
    $p_float = (float)str_replace(',', '.', $p_str); // Komma-tolerant
    if ($p_float > 0) {
        $key = number_format($p_float, 1, '.', ''); // Schlüssel normalisieren (z.B. '11' -> '11.0')
        $powerOptions[$key] = $p_float;
    }
}

if (empty($powerOptions)) {
    $powerOptions = [
        '7.2' => 7.2,
        '11.0' => 11.0,
        '22.0' => 22.0,
    ];
}
$totalCosts = [];
$totalKwhs = [];
foreach ($powerOptions as $key => $_) {
    $totalCosts[$key] = 0;
    $totalKwhs[$key] = 0;
}
$chargingSlots = 0;
$tieredPricesFallback = getTieredPrices($base_path);

// --- POST HANDLER MUSS HIER SEIN, DAMIT SCHEDULER VOR DEM LESEN DES JSONS LÄUFT ---
if (($_SERVER['REQUEST_METHOD'] ?? '') === 'POST') {
    // Frühzeitig ermitteln, ob der native Modus aktiv ist (für die Handler-Logik benötigt)
    $isNativeEnabledEarly = in_array(strtolower(trim($wallboxConfig['wb_native_enable'] ?? '0')), ['1', 'true', 'yes']);
    $abort_flag = '/var/www/html/ramdisk/native_schedule_aborted.flag';

    $quickAction = null;
    if (isset($_POST['quick_action'])) {
        $quickAction = $_POST['quick_action'];
        if ($quickAction === 'start_now') {
            $_POST['zwei'] = '99';
        } elseif ($quickAction === 'clear_times') {
            $_POST['zwei'] = '0';
        }
    }

    if (isset($_POST['zwei'])) {
        $neueDauer = trim((string)$_POST['zwei']);
        $neueDauerInt = (int)$neueDauer;

        if (!is_numeric($neueDauer) || $neueDauerInt < 0 || ($neueDauerInt > 24 && $neueDauerInt < 99) || $neueDauerInt > 99) {
            $message = errorMessage('Ungültige Eingabe', 'Die Ladedauer muss zwischen 0 und 24 Stunden oder 99 = unbegrenzt liegen.');
        } elseif ($isNativeEnabledEarly) {
            // NATIVE MODUS: Schreibe NIEMALS in e3dc.wallbox.txt!
            // Stattdessen: wbhour in config.txt setzen -> wallbox_manager übernimmt
            $wbhourVal = ($neueDauerInt === 99) ? '99' : (string)$neueDauerInt;
            // wb_sofort=1 NUR bei Max/start_now (Sofortladen ohne Zeitfenster).
            // Beim normalen Speichern des Schiebereglers gilt wb_sofort=0; die Zeitsteuerung berücksichtigt wbvon/wbbis.
            $ecoMode = isset($_POST['wb_native_eco'])
                ? (($_POST['wb_native_eco'] === '1') ? '1' : '0')
                : ($wallboxConfig['wb_native_eco'] ?? '0');
            $nativeUpdates = [
                'wbhour'       => $wbhourVal,
                'wb_sofort'    => ($quickAction === 'start_now') ? '1' : '0',
                'wb_native_eco'=> $ecoMode,
                'wb1_mode'     => '5', // Sofort bis Preislimit
            ];
            if ($quickAction === 'start_now') {
                // Sofortladen: wbvon auf aktuelle Stunde setzen, damit sofort geplant wird.
                // Netzbezug bleibt trotzdem an die Wallbox-Preisgrenze gekoppelt.
                $nativeUpdates['wbvon'] = date('H:00');
                $nativeUpdates['wb1_mode'] = '5';
                $nativeUpdates['wb2_mode'] = '5';
            } elseif ($quickAction === 'clear_times') {
                $nativeUpdates['wbvon'] = 'now';
                $nativeUpdates['wbbis'] = '00:00';
                $nativeUpdates['wb_sofort'] = '0';
                $nativeUpdates['smart_wbhour_enable'] = '0';
                $nativeUpdates['wb1_plan_hours'] = '0';
                $nativeUpdates['wb1_wbvon'] = 'now';
                $nativeUpdates['wb1_wbbis'] = '00:00';
                $nativeUpdates['wb1_smart_wbhour_enable'] = '0';
                $nativeUpdates['wb2_plan_hours'] = '0';
                $nativeUpdates['wb2_wbvon'] = 'now';
                $nativeUpdates['wb2_wbbis'] = '00:00';
                $nativeUpdates['wb2_smart_wbhour_enable'] = '0';
            }
            $tx = e3dcWallboxPlanTransaction($nativeUpdates, [
                'operation' => $neueDauerInt > 0 ? 'plan' : 'clear',
                'abort_flag' => $neueDauerInt > 0 ? 'remove' : 'create',
            ]);
            if (!empty($tx['success'])) {
                // Abbruchflag löschen: Der Benutzer hat explizit eine Ladedauer gesetzt.
                // Das Abbruchflag ist Bestandteil derselben Dateitransaktion.
                if ($quickAction === 'start_now') {
                    $message = successMessage('Sofortladen (Max) aktiviert. wallbox_manager startet die Ladung.');
                } elseif ($quickAction === 'clear_times') {
                    $message = successMessage('Laden gestoppt (wbhour=0).');
                } else {
                    $wbvonShow = $wallboxConfig['wbvon'] ?? '00:00';
                    $whenLabel = ($wbvonShow === '00:00') ? 'jederzeit (günstigste Stunden)' : 'ab ' . $wbvonShow . ' Uhr';
                    $message = successMessage('Ladedauer (' . $neueDauerInt . 'h) gespeichert. Laden ' . $whenLabel . '.');
                }

                if (false) {
                    $message = errorMessage('Einstellung gespeichert, Neuberechnung blockiert', getInstallContextDiagnostic() ?: 'Der validierte Wallbox-Planer ist nicht verfügbar.');
                }

                $wallboxConfig = parseWallboxConfigValues($config_file);
            } else {
                $message = errorMessage('Schreibfehler', 'config.txt konnte nicht geschrieben werden.');
            }
        } else {
            // ALTMODUS: e3dc.wallbox.txt schreiben (nur wenn kein nativer Modus aktiv ist!)
            $oldUmask = umask(0002);
            $writeResult = @file_put_contents($wallbox_file, $neueDauerInt . PHP_EOL, LOCK_EX);
            umask($oldUmask);

            if ($writeResult !== false) {
                if (file_exists($abort_flag)) @unlink($abort_flag);
                if ($quickAction === 'start_now') {
                    $message = successMessage('&#9889; Sofortladen (Max) gestartet.');
                } elseif ($quickAction === 'clear_times') {
                    $message = successMessage('&#9209; Laden gestoppt.');
                } else {
                    $message = successMessage('&#10003; Wallbox-Ladedauer gespeichert.');
                }

                if (false) {
                    $message = errorMessage('Einstellung gespeichert, Neuberechnung blockiert', getInstallContextDiagnostic() ?: 'Der validierte Wallbox-Planer ist nicht verfügbar.');
                }
            } else {
                $message = errorMessage('Schreibfehler', 'Datei konnte nicht geschrieben werden.');
            }
        }
    }
}

if (file_exists('/var/www/html/ramdisk/native_wallbox_schedule.json') && is_readable('/var/www/html/ramdisk/native_wallbox_schedule.json')) {
    $sourceTimestamp = filemtime('/var/www/html/ramdisk/native_wallbox_schedule.json');
    $jsonContent = file_get_contents('/var/www/html/ramdisk/native_wallbox_schedule.json');
    $scheduleData = json_decode($jsonContent, true);
    if (is_array($scheduleData)) {
        foreach ($scheduleData as $entry) {
            $ts = (int)($entry['ts'] ?? 0);
            if (!$ts) continue;

            $modeStr = $entry['mode'] ?? 'auto';
            $entryWbId = (int)($entry['wb_id'] ?? 1);
            if ($entryWbId === 2) {
                $modeStr = 'wb2-' . $modeStr;
            }
            $finalPriceCt = isset($entry['price_ct']) ? (float)$entry['price_ct'] : null;

            if ($finalPriceCt === null && isset($tieredPricesFallback)) {
                $hour = (int)date('G', $ts);
                $finalPriceCt = $tieredPricesFallback[$hour];
            }

            $finalPriceEuro = ($finalPriceCt !== null) ? ($finalPriceCt / 100.0) : 0;
            // Rohes ts mit speichern für die Textansicht
            $plannedEntries[] = ['date' => date('j.n.', $ts), 'time' => date('H:i', $ts), 'ts' => $ts, 'source' => $modeStr, 'price' => $finalPriceCt, 'wb_id' => $entryWbId];

            foreach ($powerOptions as $key => $pwr) {
                $kwh_per_slot = $pwr * 0.25;
                $totalKwhs[$key] += $kwh_per_slot;
                $totalCosts[$key] += $kwh_per_slot * $finalPriceEuro;
            }
            $chargingSlots++;
        }
    }
} elseif (file_exists($wallbox_out_file) && is_readable($wallbox_out_file)) {
    $sourceTimestamp = filemtime($wallbox_out_file);
    $wbLines = file($wallbox_out_file, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    if ($wbLines !== false) {
        foreach ($wbLines as $wbLine) {
            // Format: 11.25 1772104500 0 19.78
            $parts = preg_split('/\s+/', trim($wbLine));
            if (count($parts) >= 4) {
                $ts = (int)$parts[1];
                $mode = (int)$parts[2]; // 0 = manual, 1 = auto
                $pricePerMwh = (float)$parts[3];

                // Kostenberechnung
                $finalPriceCt = ($pricePerMwh !== null && $pricePerMwh > 0) ? applyAwattarPriceLogic($pricePerMwh, $awmwst, $awnebenkosten) : null;

                // HT-/NT-Rückfallwert, wenn die Datei keinen dynamischen Preis enthält
                if ($finalPriceCt === null && isset($tieredPricesFallback)) {
                    $hour = (int)date('G', $ts);
                    $finalPriceCt = $tieredPricesFallback[$hour];
                }

                $finalPriceEuro = ($finalPriceCt !== null) ? ($finalPriceCt / 100.0) : 0; // von ct/kWh zu €/kWh

                $plannedEntries[] = [
                    'date' => date('j.n.', $ts),
                    'time' => date('H:i', $ts),
                    'source' => ($mode === 1) ? 'auto' : 'manual',
                    'price' => $finalPriceCt
                ];

                foreach ($powerOptions as $key => $pwr) {
                    $kwh_per_slot = $pwr * 0.25;
                    $totalKwhs[$key] += $kwh_per_slot;
                    $totalCosts[$key] += $kwh_per_slot * $finalPriceEuro;
                }

                $chargingSlots++;
            } elseif (count($parts) >= 3) {
                $ts = (int)$parts[1];
                $mode = (int)$parts[2];

                $finalPriceCt = null;
                if (isset($tieredPricesFallback)) {
                    $hour = (int)date('G', $ts);
                    $finalPriceCt = $tieredPricesFallback[$hour];
                }
                $finalPriceEuro = ($finalPriceCt !== null) ? ($finalPriceCt / 100.0) : 0;

                $plannedEntries[] = ['date' => date('j.n.', $ts), 'time' => date('H:i', $ts), 'source' => ($mode === 1) ? 'auto' : 'manual', 'price' => $finalPriceCt];

                foreach ($powerOptions as $key => $pwr) {
                    $kwh_per_slot = $pwr * 0.25;
                    $totalKwhs[$key] += $kwh_per_slot;
                    $totalCosts[$key] += $kwh_per_slot * $finalPriceEuro;
                }
                $chargingSlots++;
            }
        }
    }
}

    if (isset($_POST['save_auto_settings'])) {
        $previousConfig = parseWallboxConfigValues($config_file);
        $postedWbhour = trim((string)($_POST['Wbhour'] ?? ''));
        $postedWbvon  = trim((string)($_POST['Wbvon'] ?? ''));
        $postedWbbis  = trim((string)($_POST['Wbbis'] ?? ''));
        $adjustWbvon  = isset($_POST['adjust_wbvon']) && $_POST['adjust_wbvon'] === '1';
        // Kein Zeitfenster: immer in den günstigsten Stunden laden (Octopus/24h Modus)
        $noTimeLimit  = isset($_POST['wb_no_time_limit']) && $_POST['wb_no_time_limit'] === '1';
        if ($noTimeLimit) {
            $postedWbvon = '0';
            $postedWbbis = '0';
        }

        $wallboxConfig['wbhour'] = $postedWbhour;
        $wallboxConfig['wbvon']  = $postedWbvon;
        $wallboxConfig['wbbis']  = $postedWbbis;

        if (!is_numeric($postedWbhour) || (int)$postedWbhour < 0) {
            $message = errorMessage('Ungültige Eingabe', 'Wbhour muss ein numerischer Wert >= 0 sein.');
        } else {
            $wbvonNorm = normalizeHourInput($postedWbvon);
            $wbbisNorm = normalizeHourInput($postedWbbis);

            if ($wbvonNorm === false || $wbbisNorm === false) {
                $message = errorMessage('Ungültige Zeit', 'Wbvon und Wbbis müssen als ganze Stunden 0-23 eingegeben werden (z. B. 6 oder 22).');
            } else {
                $nowMinutes = (int)date('G') * 60 + (int)date('i');
                $wbvonMinutes = parseTimeToMinutes($wbvonNorm);

                // !!!!!!! HINWEIS: HIER IST DIE SPERRE ENTFERNT !!!!!!!
                // WIR LASSEN DIE EINGABE EINFACH DURCH, AUCH WENN Wbvon IN DER VERGANGENHEIT LIEGT

                if ($adjustWbvon && $nowMinutes > $wbvonMinutes) {
                    $nextHourTs = strtotime(date('Y-m-d H:00:00')) + 3600;
                    $wbvonNorm = date('H:00', $nextHourTs);
                }

                $updates = [
                    'wbhour' => (string)(int)$postedWbhour,
                    'wbvon' => $wbvonNorm,
                    'wbbis' => $wbbisNorm,
                    'wb_sofort' => '0'
                ];

                $message_extra = "";
                // NEU: Wenn Benutzer Wbhour manuell auf 0 setzt, schalten wir die intelligente Ladezeit ab,
                // damit der Energy Manager sie nicht sofort wieder überschreibt!
                if ($postedWbhour === '0' && ($wallboxConfig['smart_wbhour_enable'] ?? '0') === '1') {
                    $updates['smart_wbhour_enable'] = '0';
                    $message_extra = " (Die dynamische Ziel-Ladung wurde deaktiviert, damit die 0 erhalten bleibt.)";
                }

                $previousWbvon = normalizeHourInput($previousConfig['wbvon']);
                $previousWbbis = normalizeHourInput($previousConfig['wbbis']);
                $isTimeChange = ($previousWbvon !== false && $previousWbbis !== false)
                    ? ($previousWbvon !== $wbvonNorm || $previousWbbis !== $wbbisNorm)
                    : true;

                $hasAutoEntries = wallboxFileHasAutomaticEntries($wallbox_file);
                $hasPreviousDayAutoEntries = wallboxFileHasAutomaticEntriesFromPreviousDay($wallbox_file);
                $newWbhourValue = (int)$postedWbhour;
                $needsSafetyReset = $newWbhourValue > 0 && ($hasPreviousDayAutoEntries || ($hasAutoEntries && $isTimeChange));
                $canWriteAutoSettings = true;

                $tx = e3dcWallboxPlanTransaction($updates, [
                    'operation' => $newWbhourValue > 0 ? 'plan' : 'clear',
                    'abort_flag' => $newWbhourValue > 0 ? 'remove' : 'create',
                ]);
                if (!empty($tx['success'])) {
                    $message = successMessage('Automatik-Einstellungen und Ladeplan wurden transaktional gespeichert.' . $message_extra);
                    $wallboxConfig = parseWallboxConfigValues($config_file);
                } else {
                    $message = errorMessage('Automatik-Einstellungen nicht gespeichert', (string)($tx['message'] ?? 'Die transaktionale Planung ist fehlgeschlagen.'));
                }
                // Der frühere 0-W-Zwischenstand mit sleep(5) ist absichtlich
                // deaktiviert: kein sichtbarer Teilzustand vor dem validierten Plan.
                $needsSafetyReset = false;
                $canWriteAutoSettings = false;

                if ($needsSafetyReset) {
                    $resetResult = false;
                    if (!$resetResult) {
                        $message = errorMessage(
                            'Sicherheits-Reset fehlgeschlagen',
                            'Wbhour konnte nicht vorab auf 0 gesetzt werden. Bitte Dateiberechtigungen prüfen.'
                        );
                        $wallboxConfig = parseWallboxConfigValues($config_file);
                        $canWriteAutoSettings = false;
                    } else {
                        // Kein Zwischenzustand und keine Wartephase.
                    }
                }

                if ($canWriteAutoSettings) {
                    $writeResult = false;
                    if ($writeResult) {
                        // Abbruchflag löschen: Der Benutzer hat neue Automatikeinstellungen gespeichert.
                        $abort_flag = '/var/www/html/ramdisk/native_schedule_aborted.flag';
                        // Flag-Änderungen erfolgen ausschließlich in der Transaktion.

                        if ($needsSafetyReset) {
                            $message = successMessage('&#10003; Automatik-Einstellungen gespeichert (Sicherheits-Reset 5s mit Wbhour=0 durchgeführt).' . $message_extra);
                        } else {
                            $message = successMessage('&#10003; Automatik-Einstellungen gespeichert. Preisorientiertes Laden aktiv.' . $message_extra);
                        }
                        $wallboxConfig = parseWallboxConfigValues($config_file);
                    } else {
                        $message = errorMessage(
                            'Schreibberechtigung fehlt',
                            'Datei: <code>' . htmlspecialchars($config_file) . '</code><br>' .
                            'Bitte Berechtigungen prüfen, z. B.:<br>' .
                            '<code>sudo chown ' . htmlspecialchars($install_user) . ':www-data ' . htmlspecialchars($config_file) . '</code><br>' .
                            '<code>sudo chmod 664 ' . htmlspecialchars($config_file) . '</code>'
                        );
                    }
                }
            }
        }
    }

if (file_exists($wallbox_file)) {
    $readCheck = checkFileAccess($wallbox_file, 'read');
    if ($readCheck === true) {
        $alleZeilen = file($wallbox_file, FILE_IGNORE_NEW_LINES);
    }
}

if (count($alleZeilen) > 0) {
    $zeile = $alleZeilen[0];
}

$formAction = getContextPageUrl('wallbox');
$nowMinutes = (int)date('G') * 60 + (int)date('i');
$currentWbvonMinutes = parseTimeToMinutes($wallboxConfig['wbvon']);
$showWbvonHint = $currentWbvonMinutes !== false && $nowMinutes > $currentWbvonMinutes;

$wbvonDisplayHour = ($currentWbvonMinutes !== false) ? (string)floor($currentWbvonMinutes / 60) : preg_replace('/[^0-9]/', '', (string)$wallboxConfig['wbvon']);
$wbbisMinutes = parseTimeToMinutes($wallboxConfig['wbbis']);
$wbbisDisplayHour = ($wbbisMinutes !== false) ? (string)floor($wbbisMinutes / 60) : preg_replace('/[^0-9]/', '', (string)$wallboxConfig['wbbis']);

// Wallbox Sessions laden (CSV) für die Historie
$wbSessionFile = '/var/www/html/data/wb_sessions.csv';
$wbSessions = [];
$totalHistoryKwh = 0;
$totalHistorySeconds = 0;

if (!file_exists($wbSessionFile) && file_exists('/var/www/html/tmp/wb_sessions.csv')) {
    $wbSessionFile = '/var/www/html/tmp/wb_sessions.csv';
}

if (file_exists($wbSessionFile)) {
    $sessionLines = file($wbSessionFile, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    // Header überspringen, rückwärts lesen (neueste zuerst)
    if ($sessionLines !== false) {
        for ($i = count($sessionLines) - 1; $i > 0; $i--) {
            $parts = explode(';', $sessionLines[$i]);
            if (count($parts) >= 4) {
                $tsStart = strtotime($parts[1]);
                $tsEnd = strtotime($parts[2]);
                $kwh = (float)$parts[3];
                if ($tsStart && $tsEnd) {
                    $wbSessions[] = [
                        'tsStart' => $tsStart,
                        'tsEnd' => $tsEnd,
                        'kwh' => $kwh
                    ];
                    $totalHistoryKwh += $kwh;
                    $totalHistorySeconds += ($tsEnd - $tsStart);
                }
            }
        }
    }
}
$totalHistoryHours = floor($totalHistorySeconds / 3600);
$totalHistoryMinutes = round(($totalHistorySeconds % 3600) / 60);

$dailyDbData = [];
$dbPath = '/var/www/html/data/e3dc_stats.db';
if (file_exists($dbPath)) {
    try {
        $db = new PDO('sqlite:' . $dbPath);
        $res = $db->query("SELECT date, pv_yield, grid_in, autarky FROM daily_stats ORDER BY date DESC LIMIT 300")->fetchAll(PDO::FETCH_ASSOC);
        foreach ($res as $dbRow) {
            $ts = strtotime($dbRow['date']);
            if ($ts) {
                $dmy = date('d.m.Y', $ts);
                $dailyDbData[$dmy] = $dbRow;
            }
        }
    } catch (Exception $e) {}
}
// Lade Live-Wallbox-Status falls JS läuft
// Der Manager schreibt nach /logs/, Rückfall auf /ramdisk/ (älteres Format)
$wb_live_session = '/var/www/html/logs/wb_live_session.json';
if (!file_exists($wb_live_session)) {
    $wb_live_session = '/var/www/html/ramdisk/wb_live_session.json';
}
$wb_live_data = null;
if (file_exists($wb_live_session)) {
    $wb_live_data = json_decode(file_get_contents($wb_live_session), true);
}

// Wallbox-Manager-Protokolle lesen
$logFile = '/var/www/html/logs/wallbox_manager.log';
$logContent = '';
if (file_exists($logFile)) {
    $lines = e3dcReadTextTailLines($logFile, 30, 512 * 1024);
    if ($lines) {
        $logContent = implode("\n", $lines);
    }
}
$wallboxGateEvents = e3dcReadWallboxCommandGateEvents(8);
$wallboxGateLast = $wallboxGateEvents[0] ?? null;
$openwbCapabilityRows = wallboxOpenwbCapabilityRows($wallboxConfig);
$e3dcCapabilityRows = wallboxE3dcCapabilityRows($wallboxConfig);
$openWbProUpdateTargets = wallboxOpenWbProUpdateTargets($confData['config'] ?? []);
$vehicleSelectOptions = buildWallboxVehicleOptions(
    $saved_cars,
    $live_cloud_vehicles,
    [$wallboxConfig['wb1_car_id'] ?? '__none', $wallboxConfig['wb2_car_id'] ?? '__none']
);
$vehicleSelectBrowserOptions = [];
foreach ($vehicleSelectOptions as $vehicleOption) {
    $vehicleOptionId = (string)($vehicleOption['id'] ?? '');
    if ($vehicleOptionId === '' || $vehicleOptionId === '__none' || $vehicleOptionId === 'none') {
        continue;
    }
    $vehicleSelectBrowserOptions[] = $vehicleOption;
}
$simpleEnergyFromMode = function($mode, int $wb = 1) use ($wallboxConfig) {
    $mode = normalizeWallboxModeValue($mode);
    if ($mode === '0') {
        $policy = strtolower(trim((string)($wallboxConfig["wb{$wb}_observe_storage_policy"] ?? 'curve')));
        return $policy === 'reserve' ? 'pv_battery' : 'pv';
    }
    if ($mode === '4' || $mode === '12') return 'pv_battery';
    if ($mode === '5') return 'grid_price';
    return 'pv';
};
$simpleReadyTime = function(int $wb) use ($wallboxConfig) {
    $mode = normalizeWallboxModeValue($wallboxConfig["wb{$wb}_mode"] ?? (($wb === 1) ? ($wallboxConfig['WBMode'] ?? '0') : '0'));
    if ($mode === '12') {
        return normalizeWallboxSimpleReadyTime($wallboxConfig["wb{$wb}_battery_departure_time"] ?? '06:30');
    }
    $fallback = ($wb === 1) ? ($wallboxConfig['wbbis'] ?? '07:00') : '07:00';
    return normalizeWallboxSimpleReadyTime($wallboxConfig["wb{$wb}_wbbis"] ?? $fallback);
};
$simplePlanHours = function(int $wb) use ($wallboxConfig) {
    $legacy = ($wb === 1) ? ($wallboxConfig['wbhour'] ?? '0') : '0';
    return max(0, min(99, (int)($wallboxConfig["wb{$wb}_plan_hours"] ?? $legacy)));
};
$simpleSmartPlanEnabled = function(int $wb) use ($wallboxConfig) {
    $legacy = ($wb === 1) ? ($wallboxConfig['smart_wbhour_enable'] ?? '0') : '0';
    return (string)($wallboxConfig["wb{$wb}_smart_wbhour_enable"] ?? $legacy) === '1';
};
$simpleIntentFromState = function($mode, $planHours, $smartPlanEnabled) {
    $mode = normalizeWallboxModeValue($mode);
    if ($mode === '0') return 'off';
    if ($mode === '5') {
        if ((int)$planHours >= 99) return 'instant';
        return $smartPlanEnabled ? 'scheduled' : 'instant';
    }
    if ($mode === '12') return 'scheduled';
    return 'surplus';
};
$simpleVehicleName = function($vehicleId) use ($vehicleSelectOptions) {
    $vehicleId = normalizeWallboxVehicleSelection($vehicleId);
    foreach ($vehicleSelectOptions as $option) {
        if (($option['id'] ?? '') === $vehicleId) return (string)($option['name'] ?? $vehicleId);
    }
    return $vehicleId === '__none' ? '' : $vehicleId;
};
$simpleVehicleSoc = function($vehicleId) use ($vehicleSelectOptions) {
    $vehicleId = normalizeWallboxVehicleSelection($vehicleId);
    foreach ($vehicleSelectOptions as $option) {
        if (($option['id'] ?? '') === $vehicleId && ($option['soc'] ?? '') !== '' && is_numeric($option['soc'])) {
            return sanitizeWallboxPercentValue($option['soc'], 0);
        }
    }
    return '';
};
$configuredHouseReserve = sanitizeWallboxPercentValue($wallboxConfig['wbminsoc'] ?? '70', 70);
$simpleHouseReserve = sanitizeWallboxHouseReserveValue($wallboxConfig['wbminsoc'] ?? '70', 70);
$e3dcHouseReserveFloor = readE3dcWallboxDischargeFloorSoc();
$houseReserveFloorNotice = wallboxHouseReserveFloorNotice($wallboxConfig['wbminsoc'] ?? '70');
$simplePriceLimit = number_format((float)str_replace(',', '.', (string)($wallboxConfig['dvcarlimit'] ?? '0.0')), 1, '.', '');
$simpleRuleText = function($mode, $energy, $intent, $ready, $price, $reserve, $planActive) {
    $mode = normalizeWallboxModeValue($mode);
    $energy = normalizeWallboxSimpleEnergyMode($energy);
    $intent = normalizeWallboxSimpleChargeIntent($intent);
    if ($intent === 'off') {
        if ($energy === 'pv_battery') {
            return 'Betriebsart: Beobachten · PV + Akku bis ' . $reserve . '% Hausakku-Reserve, keine Ladebefehle';
        }
        return 'Betriebsart: Beobachten · keine Ladebefehle, Speicher folgt eigener Regelung';
    }
    if ($intent === 'instant') {
        if ($energy === 'grid_price') {
            return 'Betriebsart: Sofort · Netz bis ' . $price . ' ct/kWh';
        }
        if ($energy === 'pv_battery') {
            return 'Betriebsart: Sofort · PV + Akku bis ' . $reserve . '% Hausakku-Reserve, Netz bleibt aus';
        }
        return 'Betriebsart: Sofort · nur PV-Überschuss';
    }
    if ($intent === 'scheduled') {
        if ($energy === 'grid_price') {
            return $planActive
                ? 'Betriebsart: Fertig bis · Netz erlaubt, günstige Zeiten bis ' . $ready
                : 'Betriebsart: Fertig bis · Netz erlaubt, Ladeplan speichern';
        }
        if ($energy === 'pv_battery') {
            return 'Betriebsart: Fertig bis · PV + Akku bis ' . $ready . ', Hausakku-Reserve ' . $reserve . '%, Netz bleibt aus';
        }
        return 'Betriebsart: Fertig bis · nur PV-Überschuss, keine Netzgarantie';
    }
    if ($energy === 'pv_battery') {
        return 'Betriebsart: Überschuss · PV + Akku bis ' . $reserve . '% Hausakku-Reserve, Netz bleibt aus';
    }
    return 'Betriebsart: Überschuss · PV nach Ladekurve, Hausakku zuerst';
};
$simpleValuesText = function($intent, $energy, $unit, $target, $targetKwh, $ready, $reserve, $price) {
    $intent = normalizeWallboxSimpleChargeIntent($intent);
    $energy = normalizeWallboxSimpleEnergyMode($energy);
    if ($intent === 'off') {
        if ($energy === 'pv_battery') {
            return 'Unterhalb ' . $reserve . '% stützt der Akku nur Hausverbrauch und Wärmepumpe; ohne Auto gilt wieder die Ladekurve';
        }
        return '';
    }
    if ($energy === 'pv_battery') {
        return 'Unterhalb ' . $reserve . '% stützt der Akku nur Hausverbrauch und Wärmepumpe';
    }
    if ($intent === 'instant') {
        return $energy === 'grid_price' ? 'Sofortlimit: Netz bis ' . $price . ' ct/kWh' : '';
    }
    $targetText = $unit === 'kwh'
        ? 'Lademenge ' . $targetKwh . ' kWh'
        : 'Auto-Ziel ' . $target . '%';
    return 'Planwerte: ' . $targetText . ' bis ' . $ready;
};
$simpleWallboxPanels = [];
if ($hasWb1) {
    $wb1SimplePlanHours = $simplePlanHours(1);
    $wb1SimplePlanActive = $simpleSmartPlanEnabled(1);
    $wb1SimpleVehicle = canonicalWallboxVehicleSelection($wallboxConfig['wb1_car_id'] ?? '__none', $saved_cars);
    $wb1SimpleEnergy = $simpleEnergyFromMode($wb1m, 1);
    $wb1SimpleIntent = $simpleIntentFromState($wb1m, $wb1SimplePlanHours, $wb1SimplePlanActive);
    $wb1SimpleUnit = normalizeWallboxSimpleTargetUnit($wallboxConfig['wb1_target_unit'] ?? $wallboxConfig['car_target_unit'] ?? 'soc');
    $wb1SimpleTarget = sanitizeWallboxPercentValue($wallboxConfig['wb1_target_soc'] ?? $wallboxConfig['car_target_soc'] ?? '80', 80);
    $wb1SimpleTargetKwh = sanitizeWallboxKwhValue($wallboxConfig['wb1_target_kwh'] ?? $wallboxConfig['car_target_kwh'] ?? '20', 20);
    $wb1SimpleReady = $simpleReadyTime(1);
    $simpleWallboxPanels[1] = [
        'name' => $wb1_name,
        'color' => 'info',
        'mode' => $wb1m,
        'native_mode' => normalizeWallboxModeValue($wb1m),
        'energy' => $wb1SimpleEnergy,
        'intent' => $wb1SimpleIntent,
        'unit' => $wb1SimpleUnit,
        'target' => $wb1SimpleTarget,
        'target_kwh' => $wb1SimpleTargetKwh,
        'ready' => $wb1SimpleReady,
        'vehicle' => $wb1SimpleVehicle,
        'vehicle_name' => $simpleVehicleName($wb1SimpleVehicle),
        'current_soc' => $simpleVehicleSoc($wb1SimpleVehicle),
        'capacity' => sanitizeWallboxKwhValue($wallboxConfig['wb1_capacity'] ?? $wallboxConfig['car_capacity'] ?? '72.0', 72.0),
        'charge_power' => sanitizeWallboxKwValue($wallboxConfig['wb1_charge_power'] ?? $wallboxConfig['car_charge_power'] ?? '11.0', 11.0),
        'max_amp' => $wb1MaxAmpLabel,
        'plan_active' => $wb1SimplePlanActive,
        'manual_pause' => $wb1ManualPause,
        'rule_text' => $simpleRuleText($wb1m, $wb1SimpleEnergy, $wb1SimpleIntent, $wb1SimpleReady, $simplePriceLimit, $simpleHouseReserve, $wb1SimplePlanActive),
        'values_text' => $simpleValuesText($wb1SimpleIntent, $wb1SimpleEnergy, $wb1SimpleUnit, $wb1SimpleTarget, $wb1SimpleTargetKwh, $wb1SimpleReady, $simpleHouseReserve, $simplePriceLimit),
    ];
}
if ($hasWb2) {
    $wb2SimplePlanHours = $simplePlanHours(2);
    $wb2SimplePlanActive = $simpleSmartPlanEnabled(2);
    $wb2SimpleVehicle = canonicalWallboxVehicleSelection($wallboxConfig['wb2_car_id'] ?? '__none', $saved_cars);
    $wb2SimpleEnergy = $simpleEnergyFromMode($wb2m, 2);
    $wb2SimpleIntent = $simpleIntentFromState($wb2m, $wb2SimplePlanHours, $wb2SimplePlanActive);
    $wb2SimpleUnit = normalizeWallboxSimpleTargetUnit($wallboxConfig['wb2_target_unit'] ?? 'soc');
    $wb2SimpleTarget = sanitizeWallboxPercentValue($wallboxConfig['wb2_target_soc'] ?? '80', 80);
    $wb2SimpleTargetKwh = sanitizeWallboxKwhValue($wallboxConfig['wb2_target_kwh'] ?? '20', 20);
    $wb2SimpleReady = $simpleReadyTime(2);
    $simpleWallboxPanels[2] = [
        'name' => $wb2_name,
        'color' => 'warning',
        'mode' => $wb2m,
        'native_mode' => normalizeWallboxModeValue($wb2m),
        'energy' => $wb2SimpleEnergy,
        'intent' => $wb2SimpleIntent,
        'unit' => $wb2SimpleUnit,
        'target' => $wb2SimpleTarget,
        'target_kwh' => $wb2SimpleTargetKwh,
        'ready' => $wb2SimpleReady,
        'vehicle' => $wb2SimpleVehicle,
        'vehicle_name' => $simpleVehicleName($wb2SimpleVehicle),
        'current_soc' => $simpleVehicleSoc($wb2SimpleVehicle),
        'capacity' => sanitizeWallboxKwhValue($wallboxConfig['wb2_capacity'] ?? '72.0', 72.0),
        'charge_power' => sanitizeWallboxKwValue($wallboxConfig['wb2_charge_power'] ?? '11.0', 11.0),
        'max_amp' => $wb2MaxAmpLabel,
        'plan_active' => $wb2SimplePlanActive,
        'manual_pause' => $wb2ManualPause,
        'rule_text' => $simpleRuleText($wb2m, $wb2SimpleEnergy, $wb2SimpleIntent, $wb2SimpleReady, $simplePriceLimit, $simpleHouseReserve, $wb2SimplePlanActive),
        'values_text' => $simpleValuesText($wb2SimpleIntent, $wb2SimpleEnergy, $wb2SimpleUnit, $wb2SimpleTarget, $wb2SimpleTargetKwh, $wb2SimpleReady, $simpleHouseReserve, $simplePriceLimit),
    ];
}

?>

<div class="px-2 pb-5">
    <div class="d-flex justify-content-between align-items-center mb-3">
        <h5 class="fw-bold m-0 text-body">Wallbox Steuerung</h5>
        <div class="d-flex align-items-center justify-content-end gap-2 flex-wrap">
            <?php foreach ($openWbProUpdateTargets as $updateTarget): ?>
                <form method="post" class="m-0 p-0 text-end" onsubmit='return confirm(<?= htmlspecialchars(json_encode('Firmware-Update für ' . $updateTarget['label'] . ' jetzt anstoßen? Die Wallbox kann währenddessen neu starten.', JSON_UNESCAPED_UNICODE), ENT_QUOTES) ?>);'>
                    <input type="hidden" name="openwb_pro_update_wb" value="<?= (int)$updateTarget['wb'] ?>">
                    <button type="submit" class="btn btn-sm btn-outline-warning shadow-sm rounded-pill px-3" title="Firmware-Update der openWB Pro über connect.php starten">
                        <i class="fas fa-cloud-upload-alt fw-bold"></i><span class="d-none d-sm-inline ms-2">Pro-Update<?= count($openWbProUpdateTargets) > 1 ? ' WB' . (int)$updateTarget['wb'] : '' ?></span>
                    </button>
                </form>
            <?php endforeach; ?>
            <form method="post" class="m-0 p-0 text-end">
                <button type="submit" name="restart_manager" value="1" class="btn btn-sm btn-outline-info shadow-sm rounded-pill px-3" title="Python Wallbox-Manager neu starten">
                    <i class="fas fa-sync-alt fw-bold"></i><span class="d-none d-sm-inline ms-2">Dienst Neustarten</span>
                </button>
            </form>
        </div>
    </div>


    <?php if (!empty($message)): ?>
        <div class="mb-3"><?= $message ?></div>
    <?php endif; ?>

    <?php if (!$hasAnyWb): ?>
        <div class="card shadow-sm border-0">
            <div class="card-body p-4">
                <div class="d-flex align-items-start gap-3">
                    <div class="icon-box bg-secondary bg-opacity-10 text-secondary flex-shrink-0" style="width:48px; height:48px; border-radius:12px; font-size:1.35rem;">
                        <i class="fas fa-charging-station"></i>
                    </div>
                    <div>
                        <h6 class="fw-bold mb-1">Keine Wallbox konfiguriert</h6>
                        <p class="text-muted mb-3">Die Wallbox-Ansicht bleibt leer, solange WB1 im Konfigurationseditor auf deaktiviert steht und keine zweite Wallbox eingerichtet ist.</p>
                        <a class="btn btn-outline-secondary btn-sm rounded-pill px-3" href="<?= htmlspecialchars(getContextPageUrl('config')) ?>">
                            <i class="fas fa-cog me-1"></i> Konfiguration öffnen
                        </a>
                    </div>
                </div>
            </div>
        </div>
    </div>
<?php return; ?>
    <?php endif; ?>

    <script>
    (function() {
        try {
            const view = window.localStorage.getItem('e3dc.wallbox.view') === 'advanced' ? 'advanced' : 'simple';
            document.documentElement.setAttribute('data-e3dc-wallbox-view', view);
        } catch (err) {
            document.documentElement.setAttribute('data-e3dc-wallbox-view', 'simple');
        }
    })();
    </script>
    <style>
        .wallbox-view-panel[hidden] { display: none !important; }
        html[data-e3dc-wallbox-view="advanced"] #wallboxSimpleView { display: none !important; }
        html[data-e3dc-wallbox-view="advanced"] #wallboxAdvancedView[hidden] { display: block !important; }
        .wallbox-simple-card .btn-group .btn { min-width: 0; }
        .wallbox-simple-intent-group .btn { white-space: normal; line-height: 1.15; padding-left: .35rem; padding-right: .35rem; }
        .wallbox-simple-card .input-group-text { background: var(--bs-tertiary-bg); }
        .wallbox-simple-card .wallbox-simple-number-field .input-group { flex-wrap: nowrap; width: auto; }
        .wallbox-simple-card .wallbox-simple-number-field .form-control { flex: 0 0 3.6rem; width: 3.6rem; min-width: 0; text-align: center; padding-left: .45rem; padding-right: .35rem; }
        .wallbox-simple-card .wallbox-simple-battery-field .form-control { flex-basis: 3.8rem; width: 3.8rem; }
        .wallbox-simple-card .wallbox-simple-number-field .input-group-text { flex: 0 0 auto; padding-left: .45rem; padding-right: .45rem; }
        .wallbox-simple-card .wallbox-simple-number-field input[type=number] { -moz-appearance: textfield; }
        .wallbox-simple-card .wallbox-simple-number-field input[type=number]::-webkit-outer-spin-button,
        .wallbox-simple-card .wallbox-simple-number-field input[type=number]::-webkit-inner-spin-button { -webkit-appearance: none; margin: 0; }
        .wallbox-simple-status { min-height: 2.2rem; }
        .wallbox-simple-card [data-simple-target-panel][hidden] { display: none !important; }
        .wallbox-pause-btn { width: 2.25rem; height: 2.25rem; display: inline-flex; align-items: center; justify-content: center; }
    </style>

    <div class="d-flex justify-content-between align-items-center gap-3 flex-wrap mb-3">
        <div class="btn-group shadow-sm" role="group" aria-label="Wallbox Ansicht wählen">
            <button type="button" class="btn btn-info fw-bold px-3" data-wallbox-view-toggle="simple" aria-pressed="true">
                <i class="fas fa-leaf me-1"></i>Einfache Ansicht
            </button>
            <button type="button" class="btn btn-outline-secondary fw-bold px-3" data-wallbox-view-toggle="advanced" aria-pressed="false">
                <i class="fas fa-sliders-h me-1"></i>Erweiterte Ansicht
            </button>
        </div>
        <span class="badge bg-secondary-subtle text-secondary border border-secondary-subtle rounded-pill px-3 py-2">
            <i class="fas fa-shield-alt me-1"></i>Hausakku bleibt geschützt
        </span>
    </div>

    <div id="wallboxSimpleView" class="wallbox-view-panel" data-wallbox-view-panel="simple">
        <div class="card shadow-sm mb-3" style="border-radius:16px;">
            <div class="card-body p-3">
                <div class="d-flex justify-content-between align-items-start gap-3 flex-wrap">
                    <div>
                        <h6 class="fw-bold mb-1 text-body">
                            <i class="fas fa-shield-alt me-2 text-info"></i>Schutz & Preis
                        </h6>
                        <div class="small text-body-secondary">
                            Diese Werte gelten für alle Wallboxen.
                        </div>
                    </div>
                    <span class="badge bg-success-subtle text-success border border-success-subtle rounded-pill px-3 py-2" data-simple-global-status>
                        Gespeichert
                    </span>
                </div>
                <div class="row g-2 mt-2 align-items-end">
                    <div class="col-12 col-md-4 col-xl-3">
                        <label class="form-label small fw-bold text-muted mb-1" title="Reserve im Hausspeicher. Unterhalb dieses Werts bleibt Energie für das Haus im Speicher.">Hausakku-Reserve</label>
                        <div class="input-group input-group-sm">
                            <input type="number" min="0" max="100" class="form-control rounded-start-pill fw-bold" data-simple-global-reserve
                                   data-e3dc-floor="<?= htmlspecialchars($e3dcHouseReserveFloor !== null ? (string)$e3dcHouseReserveFloor : '') ?>"
                                   value="<?= htmlspecialchars($simpleHouseReserve) ?>">
                            <span class="input-group-text">%</span>
                            <button class="btn btn-secondary rounded-end-pill px-3" type="button" onclick="setWallboxHouseReserve('simple')">Setzen</button>
                        </div>
                        <?php if ($houseReserveFloorNotice !== ''): ?>
                            <div class="form-text small text-warning" data-simple-reserve-floor-note>
                                <i class="fas fa-shield-alt me-1"></i><?= htmlspecialchars($houseReserveFloorNotice) ?>
                            </div>
                        <?php endif; ?>
                    </div>
                    <div class="col-12 col-md-4 col-xl-3">
                        <label class="form-label small fw-bold text-muted mb-1" title="Gilt nur für Sofortladen mit Netz. Geplante Ladungen ignorieren dieses Limit und wählen günstige Zeiten.">Sofort-Preislimit</label>
                        <div class="input-group input-group-sm">
                            <input type="number" min="0" max="200" step="0.1" class="form-control rounded-start-pill fw-bold" data-simple-global-price
                                   value="<?= htmlspecialchars($simplePriceLimit) ?>">
                            <span class="input-group-text rounded-end-pill">ct/kWh</span>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        <div class="row g-3 mb-4">
            <?php foreach ($simpleWallboxPanels as $simpleWb => $simplePanel): ?>
            <div class="<?= $hasDualWb ? 'col-12 col-xl-6' : 'col-12' ?>">
                <form action="<?= htmlspecialchars($formAction) ?>" method="post"
                      class="card shadow-sm h-100 wallbox-simple-card"
                      style="border-radius:16px; border-left:4px solid var(--bs-<?= htmlspecialchars($simplePanel['color']) ?>);"
                      data-simple-plan-active="<?= !empty($simplePanel['plan_active']) ? '1' : '0' ?>"
                      data-simple-native-mode="<?= htmlspecialchars((string)$simplePanel['native_mode']) ?>"
                      onsubmit="return confirmSimpleWallboxSubmit(this);">
                    <input type="hidden" name="save_simple_wallbox" value="1">
                    <input type="hidden" name="simple_wb_id" value="<?= (int)$simpleWb ?>">
                    <input type="hidden" name="simple_house_reserve" data-simple-house-reserve-submit value="<?= htmlspecialchars($simpleHouseReserve) ?>">
                    <input type="hidden" name="simple_price_limit" data-simple-price-limit-submit value="<?= htmlspecialchars($simplePriceLimit) ?>">
                    <div class="card-body p-3">
                        <div class="d-flex justify-content-between align-items-start gap-2 mb-3">
                            <div>
                                <h6 class="fw-bold mb-1 text-body">
                                    <i class="fas fa-charging-station me-2 text-<?= htmlspecialchars($simplePanel['color']) ?>"></i>
                                    <?= htmlspecialchars($simplePanel['name']) ?>
                                </h6>
                                <div class="small text-body-secondary">
                                    max <?= (int)$simplePanel['max_amp'] ?>A · Ziel:
                                    <?= $simplePanel['unit'] === 'kwh'
                                        ? htmlspecialchars($simplePanel['target_kwh']) . ' kWh'
                                        : htmlspecialchars($simplePanel['target']) . '%' ?>
                                </div>
                            </div>
                            <div class="d-flex align-items-center gap-2 flex-shrink-0">
                                <button type="button"
                                        class="btn btn-sm <?= !empty($simplePanel['manual_pause']) ? 'btn-warning' : 'btn-outline-secondary' ?> rounded-circle wallbox-pause-btn"
                                        data-wallbox-pause-button
                                        data-wb-id="<?= (int)$simpleWb ?>"
                                        data-paused="<?= !empty($simplePanel['manual_pause']) ? '1' : '0' ?>"
                                        title="<?= !empty($simplePanel['manual_pause']) ? 'Automatik fortsetzen' : 'Wallbox manuell pausieren' ?>"
                                        aria-label="<?= !empty($simplePanel['manual_pause']) ? 'Wallbox ' . (int)$simpleWb . ' fortsetzen' : 'Wallbox ' . (int)$simpleWb . ' pausieren' ?>">
                                    <i class="fas <?= !empty($simplePanel['manual_pause']) ? 'fa-play' : 'fa-pause' ?>"></i>
                                </button>
                                <span class="badge bg-<?= htmlspecialchars($simplePanel['color']) ?>-subtle text-<?= htmlspecialchars($simplePanel['color']) ?> border border-<?= htmlspecialchars($simplePanel['color']) ?>-subtle rounded-pill">
                                    WB<?= (int)$simpleWb ?>
                                </span>
                            </div>
                        </div>

                        <label class="form-label small fw-bold text-muted mb-1">Energie</label>
                        <div class="btn-group w-100 mb-3" role="group" aria-label="Energiequelle Wallbox <?= (int)$simpleWb ?>">
                            <input type="radio" class="btn-check" name="simple_energy_mode" id="simpleEnergy<?= (int)$simpleWb ?>Pv" value="pv" autocomplete="off" <?= $simplePanel['energy'] === 'pv' ? 'checked' : '' ?>>
                            <label class="btn btn-outline-success fw-bold" for="simpleEnergy<?= (int)$simpleWb ?>Pv" title="PV führt ruhig entlang der Ladekurve. Hausspeicher hat Vorrang.">
                                <i class="fas fa-sun me-1"></i>PV
                            </label>

                            <input type="radio" class="btn-check" name="simple_energy_mode" id="simpleEnergy<?= (int)$simpleWb ?>PvBattery" value="pv_battery" autocomplete="off" <?= $simplePanel['energy'] === 'pv_battery' ? 'checked' : '' ?>>
                            <label class="btn btn-outline-primary fw-bold" for="simpleEnergy<?= (int)$simpleWb ?>PvBattery" title="PV plus Hausspeicher bis zur Reserve. Das Auto lädt bis dahin normal; darunter stützt der Akku nur Hausverbrauch und Wärmepumpe. Netz bleibt aus.">
                                <i class="fas fa-car-battery me-1"></i>PV + Akku
                            </label>

                            <input type="radio" class="btn-check" name="simple_energy_mode" id="simpleEnergy<?= (int)$simpleWb ?>Grid" value="grid_price" autocomplete="off" <?= $simplePanel['energy'] === 'grid_price' ? 'checked' : '' ?>>
                            <label class="btn btn-outline-warning fw-bold" for="simpleEnergy<?= (int)$simpleWb ?>Grid" title="Netzstrom ist erlaubt und wird bei Automatik über günstige Zeiten geplant.">
                                <i class="fas fa-bolt me-1"></i>Netz erlaubt
                            </label>
                        </div>

                        <label class="form-label small fw-bold text-muted mb-1">Laden</label>
                        <div class="btn-group w-100 mb-3 wallbox-simple-intent-group" role="group" aria-label="Lademodus Wallbox <?= (int)$simpleWb ?>">
                            <input type="radio" class="btn-check" name="simple_charge_intent" id="simpleIntent<?= (int)$simpleWb ?>Surplus" value="surplus" autocomplete="off" <?= $simplePanel['intent'] === 'surplus' ? 'checked' : '' ?>>
                            <label class="btn btn-outline-info fw-bold" for="simpleIntent<?= (int)$simpleWb ?>Surplus" title="Lädt mit verfügbarem PV-Überschuss. Bei PV + Akku darf der Hausspeicher bis zur Reserve stützen; Netz bleibt aus.">
                                <i class="fas fa-solar-panel me-1"></i>Überschuss
                            </label>

                            <input type="radio" class="btn-check" name="simple_charge_intent" id="simpleIntent<?= (int)$simpleWb ?>Scheduled" value="scheduled" autocomplete="off" <?= $simplePanel['intent'] === 'scheduled' ? 'checked' : '' ?>>
                            <label class="btn btn-outline-primary fw-bold" for="simpleIntent<?= (int)$simpleWb ?>Scheduled" title="Plant auf Ziel und Fertig-bis-Zeit. Mit Netz erlaubt werden günstige Zeiten genutzt.">
                                <i class="fas fa-flag-checkered me-1"></i>Fertig bis
                            </label>

                            <input type="radio" class="btn-check" name="simple_charge_intent" id="simpleIntent<?= (int)$simpleWb ?>Instant" value="instant" autocomplete="off" <?= $simplePanel['intent'] === 'instant' ? 'checked' : '' ?>>
                            <label class="btn btn-outline-danger fw-bold" for="simpleIntent<?= (int)$simpleWb ?>Instant" title="Sofort starten. Bei Netz erlaubt gilt das Preislimit.">
                                <i class="fas fa-play me-1"></i>Sofort
                            </label>

                            <input type="radio" class="btn-check" name="simple_charge_intent" id="simpleIntent<?= (int)$simpleWb ?>Off" value="off" autocomplete="off" <?= $simplePanel['intent'] === 'off' ? 'checked' : '' ?>>
                            <label class="btn btn-outline-secondary fw-bold" for="simpleIntent<?= (int)$simpleWb ?>Off" title="Nur beobachten: E3DC-Control sendet keine Ladebefehle. Ladung pausieren nutzt den Pause-Button oben.">
                                <i class="fas fa-eye me-1"></i>Beobachten
                            </label>
                        </div>

                        <label class="form-label small fw-bold text-muted mb-1">Zielangabe</label>
                        <div class="btn-group w-100 mb-2" role="group" aria-label="Zielangabe Wallbox <?= (int)$simpleWb ?>">
                            <input type="radio" class="btn-check" name="simple_target_unit" id="simpleTarget<?= (int)$simpleWb ?>Soc" value="soc" autocomplete="off" <?= $simplePanel['unit'] === 'soc' ? 'checked' : '' ?>>
                            <label class="btn btn-outline-info fw-bold" for="simpleTarget<?= (int)$simpleWb ?>Soc" title="Mit Fahrzeug, Ist-SoC und Ziel-SoC planen.">
                                <i class="fas fa-percent me-1"></i>SoC %
                            </label>

                            <input type="radio" class="btn-check" name="simple_target_unit" id="simpleTarget<?= (int)$simpleWb ?>Kwh" value="kwh" autocomplete="off" <?= $simplePanel['unit'] === 'kwh' ? 'checked' : '' ?>>
                            <label class="btn btn-outline-info fw-bold" for="simpleTarget<?= (int)$simpleWb ?>Kwh" title="Direkte Lademenge ohne Fahrzeug-SoC vorgeben.">
                                <i class="fas fa-battery-half me-1"></i>kWh
                            </label>
                        </div>

                        <input type="hidden" name="simple_vehicle_name" data-simple-vehicle-name value="<?= htmlspecialchars($simplePanel['vehicle_name'], ENT_QUOTES) ?>">
                        <input type="hidden" name="simple_charge_power" data-simple-charge-power value="<?= htmlspecialchars($simplePanel['charge_power']) ?>">

                        <div class="row g-2 mb-2" data-simple-target-panel="soc">
                            <div class="col-12 col-md-4">
                                <label class="form-label small fw-bold text-muted mb-1">Fahrzeug</label>
                                <select name="simple_vehicle_id" class="form-select form-select-sm rounded-pill fw-bold simple-car-selector" data-simple-car-selector>
                                    <?php foreach ($vehicleSelectOptions as $vehicleOption): ?>
                                        <option value="<?= htmlspecialchars($vehicleOption['id']) ?>"
                                                data-name="<?= htmlspecialchars($vehicleOption['name'] ?? '', ENT_QUOTES) ?>"
                                                data-soc="<?= htmlspecialchars((string)($vehicleOption['soc'] ?? ''), ENT_QUOTES) ?>"
                                                data-capacity="<?= htmlspecialchars((string)($vehicleOption['capacity'] ?? ''), ENT_QUOTES) ?>"
                                                data-power="<?= htmlspecialchars((string)($vehicleOption['power'] ?? ''), ENT_QUOTES) ?>"
                                                data-target="<?= htmlspecialchars((string)($vehicleOption['target_soc'] ?? ''), ENT_QUOTES) ?>"
                                                <?= ($simplePanel['vehicle'] === $vehicleOption['id']) ? 'selected' : '' ?>>
                                            <?= htmlspecialchars($vehicleOption['label']) ?>
                                        </option>
                                    <?php endforeach; ?>
                                </select>
                            </div>
                            <div class="col-6 col-sm-auto wallbox-simple-number-field">
                                <label class="form-label small fw-bold text-muted mb-1">Ist-SoC</label>
                                <div class="input-group input-group-sm">
                                    <input type="number" min="0" max="100" name="simple_current_soc" class="form-control rounded-start-pill fw-bold" data-simple-current-soc placeholder="IST"
                                           value="<?= htmlspecialchars($simplePanel['current_soc']) ?>">
                                    <span class="input-group-text rounded-end-pill">%</span>
                                </div>
                            </div>
                            <div class="col-6 col-sm-auto wallbox-simple-number-field">
                                <label class="form-label small fw-bold text-muted mb-1">Auto-Ziel</label>
                                <div class="input-group input-group-sm">
                                    <input type="number" min="0" max="100" name="simple_target_soc" class="form-control rounded-start-pill fw-bold" data-simple-target-soc
                                           value="<?= htmlspecialchars($simplePanel['target']) ?>">
                                    <span class="input-group-text rounded-end-pill">%</span>
                                </div>
                            </div>
                            <div class="col-6 col-sm-auto wallbox-simple-number-field wallbox-simple-battery-field">
                                <label class="form-label small fw-bold text-muted mb-1 text-nowrap">Auto-Akku</label>
                                <div class="input-group input-group-sm">
                                    <input type="number" min="1" max="200" step="0.1" name="simple_capacity" class="form-control rounded-start-pill fw-bold" data-simple-capacity
                                           value="<?= htmlspecialchars($simplePanel['capacity']) ?>">
                                    <span class="input-group-text rounded-end-pill">kWh</span>
                                </div>
                            </div>
                        </div>

                        <div class="row g-2 mb-2" data-simple-target-panel="kwh" hidden>
                            <div class="col-6 col-md-3">
                                <label class="form-label small fw-bold text-muted mb-1">Lademenge</label>
                                <div class="input-group input-group-sm">
                                    <input type="number" min="0" max="200" step="0.1" name="simple_target_kwh" class="form-control rounded-start-pill fw-bold" data-simple-target-kwh
                                           value="<?= htmlspecialchars($simplePanel['target_kwh']) ?>">
                                    <span class="input-group-text rounded-end-pill">kWh</span>
                                </div>
                            </div>
                        </div>

                        <div class="row g-2">
                            <div class="col-6 col-md-3">
                                <label class="form-label small fw-bold text-muted mb-1">Fertig bis</label>
                                <input type="time" name="simple_ready_by" class="form-control form-control-sm rounded-pill fw-bold" data-simple-ready-by
                                       value="<?= htmlspecialchars($simplePanel['ready']) ?>">
                            </div>
                        </div>

                        <div class="d-flex justify-content-between align-items-center gap-2 mt-3 flex-wrap wallbox-simple-status">
                            <span class="small text-body-secondary" data-simple-status-text>
                                <span class="d-block fw-semibold text-body" data-simple-rule-text><?= htmlspecialchars($simplePanel['rule_text']) ?></span>
                                <span class="d-block <?= $simplePanel['values_text'] === '' ? 'd-none' : '' ?>" data-simple-values-text><?= htmlspecialchars($simplePanel['values_text']) ?></span>
                                <span class="d-block mt-1">
                                    <span class="badge bg-warning-subtle text-warning border border-warning-subtle rounded-pill <?= !empty($simplePanel['manual_pause']) ? '' : 'd-none' ?>" data-wallbox-pause-badge>Manuell pausiert</span>
                                    <span class="badge bg-success-subtle text-success border border-success-subtle rounded-pill" data-simple-mode-state>Betriebsart gespeichert</span>
                                </span>
                            </span>
                            <button type="submit" class="btn btn-<?= htmlspecialchars($simplePanel['color']) ?> btn-sm rounded-pill fw-bold px-3">
                                <i class="fas fa-calendar-check me-1"></i>Ladeplan speichern
                            </button>
                        </div>
                    </div>
                </form>
            </div>
            <?php endforeach; ?>
        </div>
    </div>

    <div id="wallboxAdvancedView" class="wallbox-view-panel" data-wallbox-view-panel="advanced" hidden>

    <div class="card shadow-sm mb-3" style="border-radius:16px; border:1px solid rgba(14,165,233,0.25);">
        <div class="card-body p-3">
            <div class="d-flex justify-content-between align-items-start gap-3 flex-wrap">
                <div>
                    <h6 class="fw-bold mb-1 text-info"><i class="fas fa-shield-halved me-2"></i>Letzter Wallbox/openWB-Befehl</h6>
                    <div class="small text-body-secondary">Wallbox Command-Gate Diagnose: letzte erlaubte oder blockierte Schreibbefehle inklusive Grund.</div>
                </div>
                <?php if ($wallboxGateLast): ?>
                    <?php $gateColor = e3dcWallboxCommandGateDecisionColor($wallboxGateLast['decision'] ?? ''); ?>
                    <span class="badge bg-<?= htmlspecialchars($gateColor) ?> rounded-pill px-3 py-2">
                        <?= htmlspecialchars(e3dcWallboxCommandGateDecisionLabel($wallboxGateLast['decision'] ?? '')) ?>
                    </span>
                <?php else: ?>
                    <span class="badge bg-secondary rounded-pill px-3 py-2">noch kein Ereignis</span>
                <?php endif; ?>
            </div>

            <?php if ($wallboxGateLast): ?>
            <div class="row g-2 mt-2 small">
                <div class="col-6 col-lg-2">
                    <div class="text-body-secondary">Zeitpunkt</div>
                    <div class="fw-bold"><?= htmlspecialchars(e3dcWallboxCommandGateTimestampText($wallboxGateLast['ts'] ?? null, true)) ?></div>
                </div>
                <div class="col-6 col-lg-2">
                    <div class="text-body-secondary">Wallbox</div>
                    <div class="fw-bold">WB<?= (int)($wallboxGateLast['wb'] ?? 0) ?></div>
                </div>
                <div class="col-6 col-lg-2">
                    <div class="text-body-secondary">Treiber</div>
                    <div class="fw-bold text-truncate"><?= htmlspecialchars($wallboxGateLast['driver'] ?? '--') ?></div>
                </div>
                <div class="col-6 col-lg-2">
                    <div class="text-body-secondary">Aktion</div>
                    <div class="fw-bold text-truncate"><?= htmlspecialchars($wallboxGateLast['action'] ?? '--') ?></div>
                </div>
                <div class="col-12 col-lg-4">
                    <div class="text-body-secondary">Grund</div>
                    <div class="fw-bold text-truncate"><?= htmlspecialchars($wallboxGateLast['reason'] ?? '--') ?></div>
                </div>
            </div>
            <details class="mt-3">
                <summary class="small text-info fw-bold" style="cursor:pointer;">Letzte Gate-Ereignisse anzeigen</summary>
                <div class="table-responsive mt-2">
                    <table class="table table-sm align-middle mb-0 small">
                        <thead>
                            <tr>
                                <th>Zeitpunkt</th>
                                <th>Entscheidung</th>
                                <th>WB</th>
                                <th>Aktion</th>
                                <th>Grund</th>
                                <th>Nutzlast</th>
                            </tr>
                        </thead>
                        <tbody>
                            <?php foreach ($wallboxGateEvents as $event): ?>
                            <tr>
                                <td class="text-nowrap"><?= htmlspecialchars(e3dcWallboxCommandGateTimestampText($event['ts'] ?? null, true)) ?></td>
                                <td><span class="badge bg-<?= htmlspecialchars(e3dcWallboxCommandGateDecisionColor($event['decision'] ?? '')) ?>"><?= htmlspecialchars(e3dcWallboxCommandGateDecisionLabel($event['decision'] ?? '')) ?></span></td>
                                <td>WB<?= (int)($event['wb'] ?? 0) ?></td>
                                <td class="text-truncate" style="max-width:180px;"><?= htmlspecialchars($event['action'] ?? '--') ?></td>
                                <td class="text-truncate" style="max-width:220px;"><?= htmlspecialchars($event['reason'] ?? '--') ?></td>
                                <td class="text-truncate" style="max-width:220px;"><?= htmlspecialchars(e3dcWallboxCommandGatePayloadText($event['payload'] ?? null)) ?></td>
                            </tr>
                            <?php endforeach; ?>
                        </tbody>
                    </table>
                </div>
            </details>
            <?php else: ?>
            <div class="small text-body-secondary mt-2">Noch kein Audit-Eintrag vorhanden. Sobald ein Treiber schreiben möchte, erscheint hier die Entscheidung des Gates.</div>
            <?php endif; ?>
        </div>
    </div>

    <?php if (!empty($e3dcCapabilityRows)): ?>
    <div class="card shadow-sm mb-3" style="border-radius:16px; border:1px solid rgba(13,202,240,0.25);">
        <div class="card-body p-3">
            <div class="d-flex justify-content-between align-items-start gap-3 flex-wrap">
                <div>
                    <h6 class="fw-bold mb-1 text-info"><i class="fas fa-charging-station me-2"></i>E3/DC-Wallbox: Familie und Backend</h6>
                    <div class="small text-body-secondary">Der gemeinsame RSCP-Transport, die erkannte beziehungsweise gew&auml;hlte Produktfamilie und das aktive Steuer-Backend werden getrennt ausgewiesen.</div>
                </div>
                <span class="badge bg-secondary bg-opacity-25 text-secondary rounded-pill px-3 py-2">Readback-Status</span>
            </div>
            <div class="row g-2 mt-2">
                <?php foreach ($e3dcCapabilityRows as $row): ?>
                <?php
                    $familyLabels = [
                        'efy' => 'E3/DC Wallbox efy',
                        'easy_connect' => 'E3/DC Easy Connect',
                        'multi_connect' => 'E3/DC Multi Connect',
                        'multi_connect_ii' => 'E3/DC Multi Connect II',
                        'unknown' => 'E3/DC Wallbox (Familie unbekannt)',
                    ];
                    $familyLabel = $familyLabels[$row['family']] ?? $familyLabels['unknown'];
                    $backendClass = $row['backend'] === 'wbchar6_compat' ? 'info' : 'secondary';
                    $backendLabel = $row['backend'] === 'wbchar6_compat'
                        ? 'E3/DC efy/Easy – WBchar6-Kompatibilitätsregelung aktiv'
                        : ($row['backend'] === 'status_only'
                            ? 'Nur Status – WBchar6 nicht gewählt; Direkte Übergänge gesperrt'
                            : $row['backend_label']);
                ?>
                <div class="col-12 col-lg-6">
                    <div class="rounded-3 p-3 h-100" style="border:1px solid rgba(108,117,125,0.24); background:rgba(108,117,125,0.06);">
                        <div class="d-flex align-items-center justify-content-between gap-2">
                            <div class="fw-bold">WB<?= (int)$row['wb'] ?> <?= htmlspecialchars($familyLabel) ?></div>
                            <span class="badge bg-<?= $backendClass ?> bg-opacity-25 text-<?= $backendClass ?> rounded-pill"><?= htmlspecialchars($backendLabel) ?></span>
                        </div>
                        <div class="small text-body-secondary mt-2">
                            Transport: <span class="fw-bold text-body">E3/DC RSCP &uuml;ber Hauskraftwerk</span><br>
                            Firmware: <span class="fw-bold text-body"><?= htmlspecialchars($row['firmware'] ?: '--') ?></span><br>
                            RSCP-Typ: <span class="fw-bold text-body"><?= $row['rscp_type'] === null ? '--' : htmlspecialchars((string)$row['rscp_type']) ?></span> <span class="text-muted">(beobachtet, keine globale Modellzuordnung)</span><br>
                            Readback: <span class="fw-bold <?= $row['readback_complete'] ? 'text-info' : 'text-warning' ?>"><?= $row['readback_complete'] ? 'Sun/Auto/Abort vorhanden und typg&uuml;ltig; Semantik separat bewertet' : 'unvollst&auml;ndig oder nicht frisch' ?></span><br>
                            Schreibvertrag: <span class="fw-bold text-info">nur Readback-Diagnose; keine direkten &Uuml;bergangsschreibbefehle</span><br>
                            Backend: <span class="fw-bold text-<?= $backendClass ?>"><?= htmlspecialchars($backendLabel) ?></span>
                            <?php if (!$row['fresh']): ?><br><span class="text-warning">Noch keine frischen E3/DC-Wallbox-Livedaten.</span><?php endif; ?>
                        </div>
                    </div>
                </div>
                <?php endforeach; ?>
            </div>
            <div class="alert alert-warning mt-3 mb-0 py-2 small">
                Drei vorhandene Bool-Readbacks beweisen allein keine Ger&auml;tesemantik. Bei einer efy ohne Fahrzeug kann der Readback einen inaktiven Schlafzustand zeigen; bis zu einem rein lesenden Wachtest bleibt sie ebenso wie Easy Connect nur f&uuml;r direkte Sun-/Auto-/Abort-Schreibvorg&auml;nge fail-closed. Die ausdr&uuml;cklich gew&auml;hlte WBchar6-Kompatibilit&auml;tsregelung f&uuml;r Modus, Strom und episodischen Start/Stop bleibt davon unber&uuml;hrt. Native Phasenumschaltung und direkter Maximalstrom bleiben gesperrt.
            </div>
        </div>
    </div>
    <?php endif; ?>

    <?php if (!empty($openwbCapabilityRows)): ?>
    <div class="card shadow-sm mb-3" style="border-radius:16px; border:1px solid rgba(168,85,247,0.25);">
        <div class="card-body p-3">
            <div class="d-flex justify-content-between align-items-start gap-3 flex-wrap">
                <div>
                    <h6 class="fw-bold mb-1" style="color:#a855f7;"><i class="fas fa-plug-circle-check me-2"></i>openWB / openWB Pro Schnittstelle</h6>
                    <div class="small text-body-secondary">openWB Pro ist ein steuerbares EVSE-Stellglied. Normale openWB wird über Sollstrom + Heartbeat geführt; PV-Logik, Zielmodus und Phasenumschaltung bleiben bei openWB.</div>
                </div>
                <span class="badge bg-secondary bg-opacity-25 text-secondary rounded-pill px-3 py-2">Capability-Check</span>
            </div>
            <div class="row g-2 mt-2">
                <?php foreach ($openwbCapabilityRows as $row): ?>
                <?php
                    $typeLabel = $row['type'] === 'openwb_pro' ? 'openWB Pro' : 'openWB';
                    $switchLabel = $row['can_switch'] ? 'Phasenumschaltung freigegeben' : 'Leistungspfad ohne Phasenbefehl';
                    $switchClass = $row['can_switch'] ? 'success' : 'warning';
                    $controlClass = in_array($row['control_level'], ['success', 'warning', 'danger', 'secondary', 'info'], true) ? $row['control_level'] : 'info';
                    if ($row['api_surface'] === 'openwb_pro_connect_php') {
                        $apiLabel = 'connect.php offiziell';
                    } elseif (strpos((string)$row['api_surface'], 'openwb_primary_simpleapi') === 0) {
                        $apiLabel = 'Primary SimpleAPI';
                    } elseif ($row['api_surface'] === 'openwb_secondary_modbus') {
                        $apiLabel = 'Modbus Secondary';
                    } elseif ($row['api_surface'] === 'openwb_secondary_set_current_heartbeat') {
                        $apiLabel = 'Sollstrom + Heartbeat';
                    } else {
                        $apiLabel = 'SimpleAPI Status';
                    }
                    $roleText = trim((string)($row['effective_role'] ?: $row['detected_role'] ?: $row['configured_role']));
                    $roleWarn = !empty($row['role_mismatch']);
                ?>
                <div class="col-12 col-lg-6">
                    <div class="rounded-3 p-3 h-100" style="border:1px solid rgba(108,117,125,0.24); background:rgba(108,117,125,0.06);">
                        <div class="d-flex align-items-center justify-content-between gap-2">
                            <div class="fw-bold">WB<?= (int)$row['wb'] ?> <?= htmlspecialchars($typeLabel) ?><?= !empty($row['chargepoint_name']) ? ' - ' . htmlspecialchars($row['chargepoint_name']) : '' ?></div>
                            <span class="badge bg-<?= $switchClass ?> bg-opacity-25 text-<?= $switchClass ?> rounded-pill"><?= htmlspecialchars($switchLabel) ?></span>
                        </div>
                        <div class="small text-body-secondary mt-2">
                            API: <span class="fw-bold text-body"><?= htmlspecialchars($apiLabel) ?></span><br>
                            Rolle: <span class="fw-bold <?= $roleWarn ? 'text-warning' : 'text-body' ?>"><?= htmlspecialchars($roleText ?: '--') ?></span><?php if ($roleWarn): ?> <span class="text-warning">(Config abweichend)</span><?php endif; ?><br>
                            Steuerung: <span class="fw-bold text-<?= htmlspecialchars($controlClass) ?>" title="<?= htmlspecialchars($row['control_detail'] ?: $row['control_label']) ?>"><?= htmlspecialchars($row['control_label']) ?></span><br>
                            <?php if (!empty($row['command_blocked']) || (int)$row['command_failure_count'] > 0): ?>
                            Befehle: <span class="fw-bold text-<?= !empty($row['command_blocked']) ? 'danger' : 'warning' ?>"><?= (int)$row['command_failure_count'] ?>/<?= max(1, (int)$row['command_failure_limit']) ?> nicht bestätigt</span><br>
                            <?php endif; ?>
                            Quelle: <span class="fw-bold text-body"><?= htmlspecialchars($row['source'] ?: '--') ?></span>
                            <?php if (!$row['fresh']): ?><br><span class="text-warning">Noch keine frischen openWB-Livedaten.</span><?php endif; ?>
                        </div>
                    </div>
                </div>
                <?php endforeach; ?>
            </div>
            <div class="alert alert-info mt-3 mb-0 py-2 small">
                Normale openWB bitte entweder autonom betreiben oder E3DC-Control als Master nutzen. Parallele Ziel-/PV-Regelungen in openWB und E3DC-Control können am Netzpunkt gegeneinander regeln.
            </div>
        </div>
    </div>
    <?php endif; ?>

    <?php
    $planInfo = function(int $wb) use ($wallboxConfig) {
        $legacyHours = ($wb === 1) ? ($wallboxConfig['wbhour'] ?? 0) : 0;
        $hoursRaw = (int)($wallboxConfig["wb{$wb}_plan_hours"] ?? $legacyHours);
        $hoursLabel = ($hoursRaw >= 99) ? 'dauerhaft' : ($hoursRaw . ' h');
        $from = $wallboxConfig["wb{$wb}_wbvon"] ?? (($wb === 1) ? ($wallboxConfig['wbvon'] ?? '00:00') : '00:00');
        $to = $wallboxConfig["wb{$wb}_wbbis"] ?? (($wb === 1) ? ($wallboxConfig['wbbis'] ?? '07:00') : '07:00');
        $fromIsNow = in_array(strtolower(trim((string)$from)), ['now', 'jetzt'], true);
        $fromLabel = $fromIsNow ? 'Jetzt' : htmlspecialchars($from);
        $toLabel = htmlspecialchars($to);
        $windowLabel = (($wallboxConfig['wb_no_time_limit'] ?? '0') === '1' || (!$fromIsNow && $fromLabel === '00:00' && $toLabel === '00:00'))
            ? '24h / günstigste Slots'
            : $fromLabel . ' - ' . $toLabel . ' Uhr';
        $smart = (($wallboxConfig["wb{$wb}_smart_wbhour_enable"] ?? (($wb === 1) ? ($wallboxConfig['smart_wbhour_enable'] ?? '0') : '0')) === '1')
            ? 'Ziel-SoC aktiv'
            : 'manuelle Stunden';
        return [$hoursRaw, $hoursLabel, $windowLabel, $smart, $from, $to];
    };
    [$wb1PlanHoursRaw, $wb1PlanHoursLabel, $wb1PlanWindowLabel, $wb1SmartPlanLabel, $wb1PlanFrom, $wb1PlanTo] = $planInfo(1);
    [$wb2PlanHoursRaw, $wb2PlanHoursLabel, $wb2PlanWindowLabel, $wb2SmartPlanLabel, $wb2PlanFrom, $wb2PlanTo] = $planInfo(2);
    $wb1TargetLabel = htmlspecialchars($wallboxConfig['wb1_target_soc'] ?? $wallboxConfig['car_target_soc'] ?? '80');
    $wb2TargetLabel = htmlspecialchars($wallboxConfig['wb2_target_soc'] ?? '80');
    $wbModeOptionsBase = [
        '0'  => ['label' => 'Aus / autonom', 'help' => 'E3DC-Control sendet keine Start- oder Strombefehle. Der Ladepunkt bleibt frei für die Wallbox, den E3DC oder ein Fremdsystem. Geplante Ladefenster werden in Aus nicht gestartet.'],
        '2'  => ['label' => 'PV-Kurve ruhig', 'help' => 'Lädt entlang der Speicher-Ladekurve mit Hysterese. Kurze Lastwechsel werden geglättet, damit das Schütz nicht flattert.'],
        '3'  => ['label' => 'Grundladung stabil', 'help' => 'Hält eine ruhige Mindestladung gegen Takten. Gestoppt wird erst, wenn wbminSoC bzw. das Speicherziel sonst nicht erreichbar bleibt.'],
        '4'  => ['label' => 'PV + Akku bis Untergrenze', 'help' => 'Das Auto darf PV und Hausakku bis zur Hausakku-Reserve nutzen. Bis zu dieser Untergrenze lädt das Auto normal. Netz bleibt aus; wenn die Wallbox mehr Leistung will, stützt der Akku nur Hausverbrauch und Wärmepumpe.'],
        '5'  => ['label' => 'Sofort bis Preislimit', 'help' => 'Startet sofort mit PV und Speicher. Netzstrom wird nur bis zur eingestellten Preisgrenze genutzt. Geplante Ladefenster bleiben davon unberührt.'],
        '12' => ['label' => 'Akku bis Abfahrt', 'help' => 'Startet im Freigabefenster bis zur Abfahrtszeit mit PV und Hausakku. Netzladen bleibt gesperrt; gestoppt wird bei Abfahrt, vollem Fahrzeug oder wbminSoC.'],
    ];
    $wb1ModeOptions = $wbModeOptionsBase;
    $wb2ModeOptions = $wbModeOptionsBase;
    $wb1ModeOptions['0'] = isE3dcNativeWallboxType($wb1_type_raw)
        ? ['label' => 'Nur beobachten, E3DC regelt', 'help' => 'E3DC-Control sendet keine Ladebefehle. Der E3DC regelt diese Wallbox selbst. Geplante Ladefenster werden in Beobachten nicht gestartet.']
        : ['label' => 'Nur beobachten, Wallbox regelt', 'help' => 'E3DC-Control sendet keine Ladebefehle. Die Wallbox oder ein anderes System regelt selbst. Geplante Ladefenster werden in Beobachten nicht gestartet.'];
    $wb2ModeOptions['0'] = isE3dcNativeWallboxType($wb2_type_raw)
        ? ['label' => 'Nur beobachten, E3DC regelt', 'help' => 'E3DC-Control sendet keine Ladebefehle. Der E3DC regelt diese Wallbox selbst. Geplante Ladefenster werden in Beobachten nicht gestartet.']
        : ['label' => 'Nur beobachten, Wallbox regelt', 'help' => 'E3DC-Control sendet keine Ladebefehle. Die Wallbox oder ein anderes System regelt selbst. Geplante Ladefenster werden in Beobachten nicht gestartet.'];
    $wb1ModeHelp = array_map(fn($entry) => $entry['help'], $wb1ModeOptions);
    $wb2ModeHelp = array_map(fn($entry) => $entry['help'], $wb2ModeOptions);
    ?>

    <?php if ($hasDualWb): ?>
    <div class="d-flex flex-column flex-lg-row justify-content-between align-items-lg-center gap-3 mb-3 p-3 rounded-3 border bg-body-tertiary">
        <div>
            <h6 class="fw-bold mb-1 text-body"><i class="fas fa-balance-scale me-2 text-info"></i>Ladepriorität</h6>
            <div class="small text-body-secondary">
                Bei knappem Budget startet die ausgewählte Wallbox zuerst; laufende Ladungen werden geordnet übergeben.
            </div>
        </div>
        <div class="d-flex flex-column align-items-lg-end gap-2">
            <div class="btn-group wb-priority-toggle" role="group" aria-label="Wallbox Ladepriorität">
                <input type="radio" class="btn-check" name="wallboxPriority" id="wbPriority1" value="1"
                       autocomplete="off" onchange="saveWbPriority(this.value)"
                       <?= $wbPriorityMode === 1 ? 'checked' : '' ?>>
                <label class="btn btn-outline-info fw-bold px-3" for="wbPriority1">WB1</label>

                <input type="radio" class="btn-check" name="wallboxPriority" id="wbPriority0" value="0"
                       autocomplete="off" onchange="saveWbPriority(this.value)"
                       <?= $wbPriorityMode === 0 ? 'checked' : '' ?>>
                <label class="btn btn-outline-secondary fw-bold px-3" for="wbPriority0">Beide</label>

                <input type="radio" class="btn-check" name="wallboxPriority" id="wbPriority2" value="2"
                       autocomplete="off" onchange="saveWbPriority(this.value)"
                       <?= $wbPriorityMode === 2 ? 'checked' : '' ?>>
                <label class="btn btn-outline-warning fw-bold px-3" for="wbPriority2">WB2</label>
            </div>
            <span id="wbPriorityStatus" class="badge bg-secondary-subtle text-secondary border border-secondary-subtle"
                  data-saved-mode="<?= (int)$wbPriorityMode ?>">
                Gespeichert
            </span>
        </div>
    </div>
    <?php endif; ?>

    <!-- NEU: Wallbox Betriebsmodus & Sperre -->
    <div class="row g-3 mb-4">
        <!-- Wallbox 1 -->
        <?php if ($hasWb1): ?>
        <div class="col-md-6">
            <div class="card shadow-sm h-100" style="border-radius: 16px; border-left: 4px solid #a855f7;">
                <div class="card-body p-3">
                    <div class="d-flex justify-content-between align-items-center mb-3 gap-2">
                        <h6 class="card-title text-body fw-bold m-0 d-flex align-items-center w-75">
                            <i class="fas fa-charging-station me-2 text-purple"></i>
                            <input type="text" value="<?= htmlspecialchars($wb1_name) ?>"
                                   class="form-control form-control-sm border-0 bg-transparent fw-bold p-0 text-body shadow-none"
                                   onchange="saveWbName(1, this.value)" placeholder="Wallbox 1" title="Klicken zum Umbenennen">
                        </h6>
                        <div class="d-flex align-items-center gap-2 flex-shrink-0">
                            <span class="badge bg-info-subtle text-info border border-info-subtle" title="Konfiguriertes Hardware-Limit der Wallbox. Änderung nur im Konfigurations-Editor.">max <?= (int)$wb1MaxAmpLabel ?>A</span>
                            <button type="button"
                                    class="btn btn-sm <?= $wb1ManualPause ? 'btn-warning' : 'btn-outline-secondary' ?> rounded-circle wallbox-pause-btn"
                                    data-wallbox-pause-button
                                    data-wb-id="1"
                                    data-paused="<?= $wb1ManualPause ? '1' : '0' ?>"
                                    title="<?= $wb1ManualPause ? 'Automatik fortsetzen' : 'Wallbox manuell pausieren' ?>"
                                    aria-label="<?= $wb1ManualPause ? 'Wallbox 1 fortsetzen' : 'Wallbox 1 pausieren' ?>">
                                <i class="fas <?= $wb1ManualPause ? 'fa-play' : 'fa-pause' ?>"></i>
                            </button>
                            <div class="form-check form-switch fs-5 m-0" title="Ladepunkt komplett sperren">
                                <input class="form-check-input" type="checkbox" role="switch" id="wb1LockSwitch" onchange="toggleWbMode(1)" <?= (isset($wallboxConfig['wb1_locked']) && $wallboxConfig['wb1_locked'] === '0') ? 'checked' : '' ?>>
                                <label class="form-check-label fs-6 ms-1" for="wb1LockSwitch" style="cursor:pointer;"><i class="fas fa-power-off opacity-75"></i></label>
                            </div>
                        </div>
                    </div>
                    <div class="d-flex align-items-center gap-2 mb-2 flex-wrap">
                        <span class="badge bg-<?= htmlspecialchars($wb1MasterInfo['class']) ?> bg-opacity-25 text-<?= htmlspecialchars($wb1MasterInfo['class']) ?> border border-<?= htmlspecialchars($wb1MasterInfo['class']) ?> border-opacity-50"
                              title="<?= htmlspecialchars($wb1MasterInfo['title']) ?>">
                            <i class="fas fa-user-shield me-1"></i><?= htmlspecialchars($wb1MasterInfo['label']) ?>
                        </span>
                        <?php if (!empty($wb1MasterInfo['local'])): ?>
                            <span class="badge bg-secondary-subtle text-secondary border border-secondary-subtle" title="<?= htmlspecialchars($wb1MasterInfo['title']) ?>">
                                <i class="fas fa-hand-pointer me-1"></i>Manuell lokal
                            </span>
                        <?php endif; ?>
                    </div>
                    <select class="form-select border-secondary shadow-sm" id="wb1ModeSelect" data-saved-mode="<?= htmlspecialchars($wb1m) ?>" onchange="toggleWbMode(1)">
                        <?php
                        foreach ($wb1ModeOptions as $val => $entry):
                        ?>
                            <option value="<?= $val ?>" data-help="<?= htmlspecialchars($entry['help'], ENT_QUOTES) ?>" <?= ($wb1m == (string)$val) ? 'selected' : '' ?>><?= htmlspecialchars($entry['label']) ?></option>
                        <?php endforeach; ?>
                    </select>
                    <div id="wb1ModeHelp" class="mt-2 p-2 rounded-3 border border-secondary-subtle bg-body-tertiary small text-body-secondary">
                        <?= htmlspecialchars($wb1ModeHelp[$wb1m] ?? reset($wb1ModeHelp)) ?>
                    </div>
                    <div id="wb1ObserveStorageBox" class="mt-2 p-2 rounded-3 border border-info-subtle bg-body-tertiary small <?= $wb1m === '0' ? '' : 'd-none' ?>">
                        <label for="wb1ObserveStoragePolicy" class="form-label fw-bold text-info mb-1">
                            <i class="fas fa-car-battery me-1"></i>Speicher bei Beobachten
                        </label>
                        <select class="form-select form-select-sm rounded-pill" id="wb1ObserveStoragePolicy" data-saved-policy="<?= htmlspecialchars($wb1ObserveStoragePolicy) ?>" onchange="toggleWbMode(1)">
                            <option value="curve" <?= $wb1ObserveStoragePolicy === 'curve' ? 'selected' : '' ?>>Nur beobachten, Speicher folgt Ladekurve</option>
                            <option value="reserve" <?= $wb1ObserveStoragePolicy === 'reserve' ? 'selected' : '' ?>>Nur beobachten, Speicher oberhalb Hausreserve freigegeben</option>
                        </select>
                        <div class="form-text small">E3DC-Control sendet keine Ladebefehle. Bei Hausreserve-Freigabe darf das Auto nur oberhalb der Reserve aus dem Speicher ziehen; ohne Auto gilt wieder die Ladekurve.</div>
                    </div>
                    <div id="wb1BatteryDepartureBox" class="mt-2 p-2 rounded-3 border border-success-subtle bg-body-tertiary small <?= $wb1m === '12' ? '' : 'd-none' ?>">
                        <label for="wb1BatteryDepartureTime" class="form-label fw-bold text-success mb-1">
                            <i class="fas fa-car-battery me-1"></i>Abfahrt
                        </label>
                        <input type="time" class="form-control form-control-sm rounded-pill" id="wb1BatteryDepartureTime"
                               value="<?= htmlspecialchars(normalizeWallboxDepartureTime($wallboxConfig['wb1_battery_departure_time'] ?? '06:30')) ?>"
                               onchange="saveWbBatteryDeparture(1)">
                        <label for="wb1BatteryDepartureWindow" class="form-label fw-bold text-success mb-1 mt-2">
                            <i class="fas fa-hourglass-half me-1"></i>Startfenster vor Abfahrt
                        </label>
                        <div class="input-group input-group-sm">
                            <input type="number" min="1" max="36" step="0.5" class="form-control rounded-start-pill" id="wb1BatteryDepartureWindow"
                                   value="<?= htmlspecialchars(normalizeWallboxDepartureWindowHours($wallboxConfig['wb1_battery_departure_window_h'] ?? '3')) ?>"
                                   onchange="saveWbBatteryDeparture(1)">
                            <span class="input-group-text rounded-end-pill">h</span>
                        </div>
                        <div class="form-text small">Nur in diesem Fenster, nur PV/Akku bis wbminSoC, kein Netzladen.</div>
                    </div>
                    <div class="mt-3 p-2 rounded-3 border border-info-subtle bg-body-tertiary small">
                        <div class="d-flex justify-content-between gap-2 flex-wrap">
                            <span class="fw-bold text-info"><i class="fas fa-calendar-alt me-1"></i>Ladeplan</span>
                            <span class="badge bg-info-subtle text-info"><?= $wb1SmartPlanLabel ?></span>
                        </div>
                        <div class="d-flex justify-content-between gap-2 mt-1">
                            <span class="text-muted">Dauer</span>
                            <strong><?= htmlspecialchars($wb1PlanHoursLabel) ?></strong>
                        </div>
                        <div class="d-flex justify-content-between gap-2">
                            <span class="text-muted">Fenster</span>
                            <strong><?= $wb1PlanWindowLabel ?></strong>
                        </div>
                        <div class="d-flex justify-content-between gap-2">
                            <span class="text-muted">Ziel</span>
                            <strong><?= $wb1TargetLabel ?>%</strong>
                        </div>
                        <a href="#load-planning-card" class="btn btn-outline-info btn-sm rounded-pill w-100 mt-2 fw-bold">
                            <i class="fas fa-sliders-h me-1"></i> Ladeplanung bearbeiten
                        </a>
                    </div>
                </div>
            </div>
        </div>
        <?php endif; ?>
        <!-- Wallbox 2 -->
        <?php if ($hasWb2): ?>
        <div class="col-md-6">
            <div class="card shadow-sm h-100" style="border-radius: 16px; border-left: 4px solid #0dcaf0;">
                <div class="card-body p-3">
                    <div class="d-flex justify-content-between align-items-center mb-3 gap-2">
                        <h6 class="card-title text-body fw-bold m-0 d-flex align-items-center w-75">
                            <i class="fas fa-charging-station me-2 text-info"></i>
                            <input type="text" value="<?= htmlspecialchars($wb2_name) ?>"
                                   class="form-control form-control-sm border-0 bg-transparent fw-bold p-0 text-body shadow-none"
                                   onchange="saveWbName(2, this.value)" placeholder="Wallbox 2" title="Klicken zum Umbenennen">
                        </h6>
                        <div class="d-flex align-items-center gap-2 flex-shrink-0">
                            <span class="badge bg-warning-subtle text-warning border border-warning-subtle" title="Konfiguriertes Hardware-Limit der Wallbox. Änderung nur im Konfigurations-Editor.">max <?= (int)$wb2MaxAmpLabel ?>A</span>
                            <button type="button"
                                    class="btn btn-sm <?= $wb2ManualPause ? 'btn-warning' : 'btn-outline-secondary' ?> rounded-circle wallbox-pause-btn"
                                    data-wallbox-pause-button
                                    data-wb-id="2"
                                    data-paused="<?= $wb2ManualPause ? '1' : '0' ?>"
                                    title="<?= $wb2ManualPause ? 'Automatik fortsetzen' : 'Wallbox manuell pausieren' ?>"
                                    aria-label="<?= $wb2ManualPause ? 'Wallbox 2 fortsetzen' : 'Wallbox 2 pausieren' ?>">
                                <i class="fas <?= $wb2ManualPause ? 'fa-play' : 'fa-pause' ?>"></i>
                            </button>
                            <div class="form-check form-switch fs-5 m-0" title="Ladepunkt komplett sperren">
                                <input class="form-check-input" type="checkbox" role="switch" id="wb2LockSwitch" onchange="toggleWbMode(2)" <?= (isset($wallboxConfig['wb2_locked']) && $wallboxConfig['wb2_locked'] === '0') ? 'checked' : '' ?>>
                                <label class="form-check-label fs-6 ms-1" for="wb2LockSwitch" style="cursor:pointer;"><i class="fas fa-power-off opacity-75"></i></label>
                            </div>
                        </div>
                    </div>
                    <div class="d-flex align-items-center gap-2 mb-2 flex-wrap">
                        <span class="badge bg-<?= htmlspecialchars($wb2MasterInfo['class']) ?> bg-opacity-25 text-<?= htmlspecialchars($wb2MasterInfo['class']) ?> border border-<?= htmlspecialchars($wb2MasterInfo['class']) ?> border-opacity-50"
                              title="<?= htmlspecialchars($wb2MasterInfo['title']) ?>">
                            <i class="fas fa-user-shield me-1"></i><?= htmlspecialchars($wb2MasterInfo['label']) ?>
                        </span>
                        <?php if (!empty($wb2MasterInfo['local'])): ?>
                            <span class="badge bg-secondary-subtle text-secondary border border-secondary-subtle" title="<?= htmlspecialchars($wb2MasterInfo['title']) ?>">
                                <i class="fas fa-hand-pointer me-1"></i>Manuell lokal
                            </span>
                        <?php endif; ?>
                    </div>
                    <select class="form-select border-secondary shadow-sm" id="wb2ModeSelect" data-saved-mode="<?= htmlspecialchars($wb2m) ?>" onchange="toggleWbMode(2)">
                        <?php
                        foreach ($wb2ModeOptions as $val => $entry):
                        ?>
                            <option value="<?= $val ?>" data-help="<?= htmlspecialchars($entry['help'], ENT_QUOTES) ?>" <?= ($wb2m == (string)$val) ? 'selected' : '' ?>><?= htmlspecialchars($entry['label']) ?></option>
                        <?php endforeach; ?>
                    </select>
                    <div id="wb2ModeHelp" class="mt-2 p-2 rounded-3 border border-secondary-subtle bg-body-tertiary small text-body-secondary">
                        <?= htmlspecialchars($wb2ModeHelp[$wb2m] ?? reset($wb2ModeHelp)) ?>
                    </div>
                    <div id="wb2ObserveStorageBox" class="mt-2 p-2 rounded-3 border border-info-subtle bg-body-tertiary small <?= $wb2m === '0' ? '' : 'd-none' ?>">
                        <label for="wb2ObserveStoragePolicy" class="form-label fw-bold text-info mb-1">
                            <i class="fas fa-car-battery me-1"></i>Speicher bei Beobachten
                        </label>
                        <select class="form-select form-select-sm rounded-pill" id="wb2ObserveStoragePolicy" data-saved-policy="<?= htmlspecialchars($wb2ObserveStoragePolicy) ?>" onchange="toggleWbMode(2)">
                            <option value="curve" <?= $wb2ObserveStoragePolicy === 'curve' ? 'selected' : '' ?>>Nur beobachten, Speicher folgt Ladekurve</option>
                            <option value="reserve" <?= $wb2ObserveStoragePolicy === 'reserve' ? 'selected' : '' ?>>Nur beobachten, Speicher oberhalb Hausreserve freigegeben</option>
                        </select>
                        <div class="form-text small">E3DC-Control sendet keine Ladebefehle. Bei Hausreserve-Freigabe darf das Auto nur oberhalb der Reserve aus dem Speicher ziehen; ohne Auto gilt wieder die Ladekurve.</div>
                    </div>
                    <div id="wb2BatteryDepartureBox" class="mt-2 p-2 rounded-3 border border-success-subtle bg-body-tertiary small <?= $wb2m === '12' ? '' : 'd-none' ?>">
                        <label for="wb2BatteryDepartureTime" class="form-label fw-bold text-success mb-1">
                            <i class="fas fa-car-battery me-1"></i>Abfahrt
                        </label>
                        <input type="time" class="form-control form-control-sm rounded-pill" id="wb2BatteryDepartureTime"
                               value="<?= htmlspecialchars(normalizeWallboxDepartureTime($wallboxConfig['wb2_battery_departure_time'] ?? '06:30')) ?>"
                               onchange="saveWbBatteryDeparture(2)">
                        <label for="wb2BatteryDepartureWindow" class="form-label fw-bold text-success mb-1 mt-2">
                            <i class="fas fa-hourglass-half me-1"></i>Startfenster vor Abfahrt
                        </label>
                        <div class="input-group input-group-sm">
                            <input type="number" min="1" max="36" step="0.5" class="form-control rounded-start-pill" id="wb2BatteryDepartureWindow"
                                   value="<?= htmlspecialchars(normalizeWallboxDepartureWindowHours($wallboxConfig['wb2_battery_departure_window_h'] ?? '3')) ?>"
                                   onchange="saveWbBatteryDeparture(2)">
                            <span class="input-group-text rounded-end-pill">h</span>
                        </div>
                        <div class="form-text small">Nur in diesem Fenster, nur PV/Akku bis wbminSoC, kein Netzladen.</div>
                    </div>
                    <div class="mt-3 p-2 rounded-3 border border-warning-subtle bg-body-tertiary small">
                        <div class="d-flex justify-content-between gap-2 flex-wrap">
                            <span class="fw-bold text-warning"><i class="fas fa-calendar-alt me-1"></i>Ladeplan</span>
                            <span class="badge bg-warning-subtle text-warning"><?= $wb2SmartPlanLabel ?></span>
                        </div>
                        <div class="d-flex justify-content-between gap-2 mt-1">
                            <span class="text-muted">Dauer</span>
                            <strong><?= htmlspecialchars($wb2PlanHoursLabel) ?></strong>
                        </div>
                        <div class="d-flex justify-content-between gap-2">
                            <span class="text-muted">Fenster</span>
                            <strong><?= $wb2PlanWindowLabel ?></strong>
                        </div>
                        <div class="d-flex justify-content-between gap-2">
                            <span class="text-muted">Ziel</span>
                            <strong><?= $wb2TargetLabel ?>%</strong>
                        </div>
                        <a href="#load-planning-card" class="btn btn-outline-warning btn-sm rounded-pill w-100 mt-2 fw-bold">
                            <i class="fas fa-sliders-h me-1"></i> Ladeplanung bearbeiten
                        </a>
                    </div>
                </div>
            </div>
        </div>
        <?php endif; ?>
    </div>

    <div class="card shadow-sm mb-4" id="load-planning-card" style="border-radius: 16px;">
        <div class="card-body p-3">
            <div class="d-flex justify-content-between align-items-center mb-3">
                <h6 class="card-title text-info fw-bold m-0"><i class="fas fa-calendar-alt me-2"></i>Ladeplanung</h6>
                <button type="button" class="btn btn-sm btn-outline-info rounded-pill fw-bold px-3 shadow-sm" onclick="document.getElementById('wbHistoryOverlay').style.display='flex'">
                    <i class="fas fa-history me-1"></i>Historie
                </button>
            </div>

            <style>
                .slot-active-auto { background-color: #0dcaf0 !important; opacity: 0.8; }
                .slot-active-manual { background-color: #ffc107 !important; opacity: 0.8; }
                .slot-active-wb2-auto { background-color: #a78bfa !important; opacity: 0.85; }
                .slot-active-wb2-manual { background-color: #fb923c !important; opacity: 0.85; }
                .slot-active-both { background: linear-gradient(90deg, #0dcaf0 0 50%, #a78bfa 50% 100%) !important; opacity: 0.9; }
                .vehicle-template-table {
                    --bs-table-bg: var(--bs-body-bg);
                    --bs-table-color: var(--bs-body-color);
                    --bs-table-border-color: var(--bs-border-color);
                    border: 1px solid var(--bs-border-color);
                }
                .vehicle-template-table thead {
                    --bs-table-bg: var(--bs-tertiary-bg);
                    color: var(--bs-body-color);
                }
                .vehicle-template-table tbody tr:hover {
                    --bs-table-hover-bg: rgba(13, 202, 240, 0.08);
                }
            </style>
            <div class="wb-timeline-container" style="background: var(--bs-tertiary-bg); border-radius: 12px; padding: 15px; border: 1px solid var(--bs-border-color);">
                <?php
                // Zeit-Berechnung für rollierende Ansicht (Now zentriert)
                $tsNow = time();
                // Auf 15 Min runden für das Raster
                $tsNowAligned = floor($tsNow / 900) * 900;
                // Startzeit: 12h vor "Jetzt"
                $tsStart = $tsNowAligned - (12 * 3600);

                // Labels generieren (-12h, -6h, Now, +6h, +12h)
                $tlLabels = [];
                for ($k=0; $k<=4; $k++) {
                    $tlLabels[] = date('H:i', $tsStart + ($k * 6 * 3600));
                }
                ?>
                <div class="timeline-labels" style="display: flex; justify-content: space-between; margin-bottom: 5px; color: var(--bs-secondary-color); font-size: 0.7rem; font-weight: bold;">
                    <span style="width: 30px; text-align: left;"><?= $tlLabels[0] ?></span>
                    <span><?= $tlLabels[1] ?></span>
                    <span style="color: #ff4757;"><?= $tlLabels[2] ?></span>
                    <span><?= $tlLabels[3] ?></span>
                    <span style="width: 30px; text-align: right;"><?= $tlLabels[4] ?></span>
                </div>

                <div class="wb-timeline-track" style="height: 35px; background: var(--bs-body-bg); border-radius: 8px; position: relative; overflow: hidden; display: flex; border: 1px solid var(--bs-border-color);">
                    <?php
                    // 96 Slots à 15 Minuten (24h Fenster)
                    for ($i = 0; $i < 96; $i++) {
                        $slotTs = $tsStart + ($i * 900);
                        $slotDate = date('j.n.', $slotTs);
                        $slotTime = date('H:i', $slotTs);

                        $sourceClass = '';
                        $slotPrice = null;
                        $slotEntriesForCell = [];
                        $slotWbIds = [];
                        if (isset($plannedEntries) && is_array($plannedEntries)) {
                            foreach ($plannedEntries as $entry) {
                                if ($entry['date'] === $slotDate && $entry['time'] === $slotTime) {
                                    $slotEntriesForCell[] = $entry;
                                    $slotPrice = $slotPrice ?? ($entry['price'] ?? null);
                                    $entryWbId = (int)($entry['wb_id'] ?? 1);
                                    if ($entryWbId !== 1 && $entryWbId !== 2) $entryWbId = 1;
                                    $slotWbIds[$entryWbId] = true;
                                }
                            }
                        }
                        if (!empty($slotEntriesForCell)) {
                            if (isset($slotWbIds[1]) && isset($slotWbIds[2])) {
                                $sourceClass = 'slot-active-both';
                            } else {
                                $firstSource = preg_replace('/[^a-z0-9_-]/i', '', (string)($slotEntriesForCell[0]['source'] ?? 'auto'));
                                $sourceClass = 'slot-active-' . $firstSource;
                            }
                        }

                        // Tooltip erstellen
                        $tooltip = $slotTime . ' Uhr';
                        if (!empty($slotWbIds)) {
                            $tooltip .= ' | ' . implode('+', array_map(fn($wbId) => 'WB' . $wbId, array_keys($slotWbIds)));
                        }
                        if ($slotPrice !== null) {
                            $tooltip .= ' | ' . number_format($slotPrice, 2, ',', '.') . ' ct/kWh';
                        }

                        // Tageswechsel markieren (wenn 00:00 Uhr)
                        $borderStyle = "border-right: 1px solid rgba(45, 55, 72, 0.3);";
                        if ($slotTime === '00:00') {
                            $borderStyle = "border-left: 1px dashed #666; border-right: 1px solid rgba(45, 55, 72, 0.3);";
                        }

                        echo '<div class="timeline-slot '.$sourceClass.'" style="flex: 1; height: 100%; '.$borderStyle.'" data-bs-toggle="tooltip" data-bs-placement="top" title="'.htmlspecialchars($tooltip, ENT_QUOTES).'" tabindex="0"></div>';
                    }
                    // Marker dynamisch platzieren (und später per JS aktualisieren)
                    $pctNow = (($tsNow - $tsStart) / (24 * 3600)) * 100;
                    if ($pctNow < 0) $pctNow = 0;
                    if ($pctNow > 100) $pctNow = 100;
                    ?>
                    <!-- Marker dynamisch (exakte Zeit) -->
                    <div id="timeline-live-marker" style="position: absolute; top: 0; bottom: 0; width: 2px; background: #ff4757; z-index: 10; left: <?= $pctNow ?>%; box-shadow: 0 0 8px rgba(255, 71, 87, 0.6); transition: left 1s linear;"></div>
                </div>

                <script>
                (function() {
                    const tsStart = <?= $tsStart ?>;
                    setInterval(() => {
                        const nowTs = Math.floor(Date.now() / 1000);
                        let pct = ((nowTs - tsStart) / (24 * 3600)) * 100;
                        if (pct > 100) pct = 100;
                        const marker = document.getElementById('timeline-live-marker');
                        if (marker) marker.style.left = pct + '%';
                    }, 10000); // Alle 10 Sekunden den Marker bewegen
                })();
                </script>

                <div class="d-flex justify-content-center gap-3 mt-3" style="font-size: 0.75rem;">
                    <span><i class="fas fa-square text-info"></i> Automatik</span>
                    <span><i class="fas fa-square text-warning"></i> Direkt</span>
                    <?php if ($hasWb2): ?><span><i class="fas fa-square" style="color:#a78bfa;"></i> WB2</span><?php endif; ?>
                    <?php if ($hasWb2): ?><span><span style="display:inline-block;width:.75em;height:.75em;border-radius:2px;background:linear-gradient(90deg,#0dcaf0 0 50%,#a78bfa 50% 100%);"></span> WB1+WB2</span><?php endif; ?>
                    <span><i class="fas fa-minus" style="color: #ff4757;"></i> Jetzt</span>
                </div>
            </div>

            <?php
            // Textuelle Zusammenfassung der Ladezeiten (zusammenhängende Slots verbinden)
            $planTextParts = [];
            if (!empty($plannedEntries)) {
                // Direkt den gespeicherten ts-Wert verwenden (europäisches Datumsformat ist mit strtotime unzuverlässig)
                $slotTsByWb = [];
                foreach ($plannedEntries as $pe) {
                    $wbId = (int)($pe['wb_id'] ?? 1);
                    if ($wbId !== 1 && $wbId !== 2) $wbId = 1;
                    if (isset($pe['ts']) && $pe['ts'] > 0) {
                        $slotTsByWb[$wbId][] = (int)$pe['ts'];
                    } else {
                        // Rückfallwert für .out-Einträge
                        $t = @strtotime(date('Y') . '-' . str_replace('.', '-', strrev(str_replace('.', '.-', strrev(trim($pe['date'], '.'))))).  ' ' . $pe['time']);
                        if ($t) $slotTsByWb[$wbId][] = $t;
                    }
                }
                ksort($slotTsByWb);
                $now_ = time();
                $today = date('Y-m-d');
                $tomorrow = date('Y-m-d', strtotime('tomorrow'));
                foreach ($slotTsByWb as $wbId => $slotTs) {
                    sort($slotTs);
                    $slotTs = array_unique($slotTs);
                    // Nur zukünftige Slots
                    $future = array_values(array_filter($slotTs, fn($t) => $t >= $now_));
                    if (empty($future)) continue;
                    $ranges = [];
                    $rStart = $future[0]; $rEnd = $future[0];
                    for ($fi = 1; $fi < count($future); $fi++) {
                        if ($future[$fi] - $rEnd <= 900) {
                            $rEnd = $future[$fi];
                        } else {
                            $ranges[] = [$rStart, $rEnd];
                            $rStart = $future[$fi]; $rEnd = $future[$fi];
                        }
                    }
                    $ranges[] = [$rStart, $rEnd];

                    // Gruppen nach Tag (nur jederst Tag-Label einmal)
                    $byDay = [];
                    foreach ($ranges as $r) {
                        $slotDay = date('Y-m-d', $r[0]);
                        if ($slotDay === $today) {
                            $dayLabel = 'heute';
                        } elseif ($slotDay === $tomorrow) {
                            $dayLabel = 'morgen';
                        } else {
                            $dayLabel = date('j.n.', $r[0]);
                        }
                        $startFmt = ltrim(date('G:i', $r[0]), '0') ?: '0';
                        if (substr($startFmt, 0, 1) === ':') $startFmt = '0' . $startFmt;
                        if ($r[0] === $r[1]) {
                            $byDay[$dayLabel][] = $startFmt;
                        } else {
                            $endFmt = ltrim(date('G:i', $r[1] + 900), '0') ?: '0';
                            if (substr($endFmt, 0, 1) === ':') $endFmt = '0' . $endFmt;
                            $byDay[$dayLabel][] = "$startFmt-$endFmt";
                        }
                    }
                    // Satz bauen: "morgen 0:45, 1:30 und 3:00–3:30"
                    $wbTextParts = [];
                    foreach ($byDay as $dayLabel => $times) {
                        $count_t = count($times);
                        if ($count_t === 1) {
                            $wbTextParts[] = "$dayLabel $times[0]";
                        } elseif ($count_t === 2) {
                            $wbTextParts[] = "$dayLabel $times[0] und $times[1]";
                        } else {
                            $last_t = array_pop($times);
                            $wbTextParts[] = "$dayLabel " . implode(', ', $times) . " und $last_t";
                        }
                    }
                    if (!empty($wbTextParts)) {
                        $prefix = $hasWb2 ? ('WB' . (int)$wbId . ': ') : '';
                        $planTextParts[] = $prefix . implode(', ', $wbTextParts);
                    }
                }
            }
            ?>

            <?php if (!empty($planTextParts)): ?>
            <div class="mt-2 d-flex justify-content-between align-items-center">
                <div></div>
                <button type="button" class="btn btn-link btn-sm text-info p-0 fw-bold" id="planTextToggle"
                    onclick="const el=document.getElementById('planTextBox');el.style.display=el.style.display==='none'?'block':'none';this.innerHTML=(el.style.display==='none'?'<i class=\'fas fa-list-ul me-1\'></i>Ladezeiten anzeigen':'<i class=\'fas fa-chevron-up me-1\'></i>Ausblenden');">
                    <i class="fas fa-list-ul me-1"></i>Ladezeiten anzeigen
                </button>
            </div>
            <div id="planTextBox" style="display:none;" class="mt-2 rounded-3 p-3 bg-body-secondary">
                <p class="mb-0 text-body" style="font-size:0.9rem;">
                    <?php
                    $count = count($planTextParts);
                    if ($count === 1) {
                        echo "Geladen wird " . $planTextParts[0] . ".";
                    } else {
                        $last = array_pop($planTextParts);
                        echo "Geladen wird " . implode(", ", $planTextParts) . " und " . $last . ".";
                    }
                    ?>
                    Falls vorhanden, wird mit PV-&Uuml;berschuss geladen.
                </p>
            </div>
            <?php endif; ?>

        </div>
    </div>

    <?php if ($chargingSlots > 0): ?>
    <div class="card shadow-sm mb-4" style="border-radius: 16px;">
        <div class="card-body p-3">
            <div class="d-flex justify-content-between align-items-center mb-3">
                <h6 class="card-title text-success fw-bold m-0"><i class="fas fa-euro-sign me-2"></i>Geschätzte Ladekosten</h6>
                <div class="btn-group btn-group-sm" role="group" id="cost-power-selector">
                    <?php foreach ($powerOptions as $key => $pwr): ?>
                        <button type="button" class="btn btn-outline-info" data-power-key="<?= $key ?>"><?= number_format($pwr, 1, ',', '.') ?> kW</button>
                    <?php endforeach; ?>
                </div>
            </div>
            <p class="small text-muted mb-3">Basierend auf <?= $chargingSlots ?> geplanten Ladefenstern (je 15 Min.).</p>

            <div id="cost-details-container">
                <?php foreach ($powerOptions as $key => $pwr): ?>
                    <div class="list-group-item bg-transparent justify-content-between align-items-center px-0 cost-detail" id="cost-detail-<?= str_replace('.', '_', $key) ?>" style="display: none;">
                        <div>
                            <strong class="text-body">Bei <?= number_format($pwr, 1, ',', '.') ?> kW Ladeleistung</strong>
                            <small class="d-block text-muted"><?= number_format($totalKwhs[$key], 2, ',', '.') ?> kWh geladen</small>
                        </div>
                        <span class="badge bg-success rounded-pill fs-6"><?= number_format($totalCosts[$key], 2, ',', '.') ?> €</span>
                    </div>
                    <?php $isFirst = false; ?>
                <?php endforeach; ?>
            </div>
        </div>
    </div>
    <?php endif; ?>

    <!-- Ladeplanung Abbrechen & NOT-AUS -->
    <?php
    $abortFlagActive = file_exists('/var/www/html/ramdisk/native_schedule_aborted.flag');
    $abortFlagTime   = $abortFlagActive ? date('H:i', filemtime('/var/www/html/ramdisk/native_schedule_aborted.flag')) : '';
    ?>
    <div class="card shadow-sm mb-4" style="border-radius:16px; background:rgba(255,193,7,0.05); border: 1px solid <?= $abortFlagActive ? 'rgba(220,53,69,0.5)' : 'rgba(255,193,7,0.35)' ?>;">
        <div class="card-body p-3">
            <h6 class="fw-bold mb-1" style="color:#d97706;"><i class="fas fa-calendar-times me-2"></i>Ladeplan &amp; Sofortladung steuern</h6>

            <?php if ($abortFlagActive): ?>
            <div class="alert alert-warning border-0 rounded-3 py-2 px-3 mb-3 d-flex align-items-center gap-3" style="font-size:0.87rem;">
                <i class="fas fa-pause-circle fs-5 text-warning flex-shrink-0"></i>
                <div>
                    <strong>Automatik pausiert</strong> seit <?= $abortFlagTime ?> Uhr &mdash; kein automatischer Netz-Ladeplan. PV-Laden läuft weiter.
                    <br><small class="text-muted">Klicke "Automatik starten" um einen <strong>neuen</strong> Plan basierend auf aktuellem SoC und Abfahrtszeit zu erstellen.</small>
                </div>
            </div>
            <form action="<?= htmlspecialchars($formAction) ?>" method="post" class="mb-3">
                <button type="submit" name="recreate_plan" value="1"
                        class="btn btn-success w-100 rounded-pill fw-bold py-2 shadow-sm">
                    <i class="fas fa-play-circle me-2"></i> Automatik starten (neuen Plan erstellen)
                </button>
            </form>
            <?php else: ?>
            <p class="small text-muted mb-3" style="font-size:0.82rem;">
                <strong>Plan l&ouml;schen</strong> stoppt nur den Netz-Ladeplan &mdash; PV-&Uuml;berschussladen l&auml;uft unver&auml;ndert weiter.<br>
                <strong>NOT-AUS</strong> sperrt die Wallbox physisch &mdash; kein Laden mehr bis zur manuellen Freigabe.
            </p>
            <?php endif; ?>

            <div class="row g-2">
                <div class="col-12 col-sm-7">
                    <form action="<?= htmlspecialchars($formAction) ?>" method="post"
                          onsubmit="return confirm('Netz-Ladeplan l&ouml;schen? PV-Laden l&auml;uft weiter.');"
                          title="Nur Netz-Ladeplanung stoppen &ndash; Wallbox bleibt aktiv">
                        <button type="submit" name="abort_charging"
                                class="btn btn-outline-warning w-100 rounded-pill fw-bold py-2 shadow-sm"
                                id="btn-abort-charging">
                            <i class="fas fa-calendar-times me-2"></i> Plan l&ouml;schen (PV l&auml;uft weiter)
                        </button>
                    </form>
                </div>
                <div class="col-12 col-sm-5">
                    <form action="<?= htmlspecialchars($formAction) ?>" method="post"
                          onsubmit="return confirm('&#9888; NOT-AUS: Wallbox wird GESPERRT. Kein Laden bis zur manuellen Freigabe. Wirklich?');"
                          title="Physische Sperre &ndash; kein Laden mehr m&ouml;glich">
                        <button type="submit" name="abort_all_charging"
                                class="btn btn-danger w-100 rounded-pill fw-bold py-2 shadow-sm"
                                id="btn-not-aus">
                            <i class="fas fa-power-off me-2"></i> NOT-AUS
                        </button>
                    </form>
                </div>
            </div>
        </div>
    </div>

    <?php
    $isNativeEnabled = (isset($wallboxConfig['wb_native_enable']) && in_array(strtolower(trim($wallboxConfig['wb_native_enable'])), ['1', 'true', 'yes']));
    if (!$isNativeEnabled):
    ?>
    <div class="row g-4 mb-4">
        <!-- Direktsteuerung -->
        <div class="col-12 col-lg-6">
            <div class="card shadow-sm h-100" style="border-radius: 16px;">
                <div class="card-body p-3">
                    <h6 class="card-title text-warning fw-bold mb-3"><i class="fas fa-bolt me-2"></i>Direktsteuerung (Sofort)</h6>

                    <form action="<?= htmlspecialchars($formAction) ?>" method="post">
                        <div class="mb-3">
                            <label class="form-label text-muted small fw-bold d-flex justify-content-between mb-1">
                                <span>Ladedauer (Stunden)</span>
                                <span id="zweiVal" class="badge <?= trim($zeile) == '99' ? 'bg-primary' : 'bg-secondary' ?> text-white">
                                    <?= trim($zeile) == '99' ? 'Dauerhaft (99 h)' : htmlspecialchars(trim($zeile)) . ' h' ?>
                                </span>
                            </label>
                            <input type="hidden" name="zwei" id="zweiHidden" value="<?= htmlspecialchars(trim($zeile)) ?>">
                            <input type="range" class="form-range"
                                value="<?= trim($zeile) == '99' ? '24' : htmlspecialchars(trim($zeile)) ?>" min="0" max="24" step="1"
                                oninput="document.getElementById('zweiHidden').value = this.value; document.getElementById('zweiVal').innerText = this.value + ' h'; document.getElementById('zweiVal').className = 'badge bg-secondary text-white';">
                            <div class="form-text text-muted small">Max (99) Dauerhaft / 0 = Stop/Löschen</div>
                        </div>

                        <div class="row g-2 mb-3">
                            <div class="col-6">
                                <button type="submit" name="quick_action" value="clear_times" class="btn btn-outline-danger w-100 btn-sm py-2 border-2 fw-bold rounded-pill">
                                    <i class="fas fa-stop"></i> Stop (0)
                                </button>
                            </div>
                            <div class="col-6">
                                <button type="submit" name="quick_action" value="start_now" class="btn btn-outline-warning w-100 btn-sm py-2 border-2 fw-bold rounded-pill">
                                    <i class="fas fa-bolt"></i> Max (99)
                                </button>
                            </div>
                        </div>

                        <button type="submit" class="btn btn-outline-warning w-100 rounded-pill fw-bold border-2">
                            ✓ Ladedauer speichern
                        </button>
                    </form>
                </div>
            </div>
        </div>

        <!-- Automatik Steuerung -->
        <div class="col-12 col-lg-6">
            <div class="card shadow-sm h-100" style="border-radius: 16px;">
                <div class="card-body p-3">
                    <h6 class="card-title text-info fw-bold mb-3"><i class="fas fa-clock me-2"></i>Automatik Steuerung</h6>

                    <form action="<?= htmlspecialchars($formAction) ?>" method="post">
                        <div class="mb-3">
                            <label for="Wbhour" class="form-label text-muted small fw-bold d-flex justify-content-between mb-1">
                                <span>Ladedauer (Wbhour)</span>
                                <span id="wbhourVal" class="badge bg-secondary text-white"><?= htmlspecialchars($wallboxConfig['wbhour']) ?> h</span>
                            </label>
                            <input type="range" id="Wbhour" name="Wbhour" class="form-range"
                                   value="<?= htmlspecialchars($wallboxConfig['wbhour']) ?>" min="0" max="24" step="1" oninput="document.getElementById('wbhourVal').innerText = this.value + ' h';">
                        </div>

                        <div class="form-check mb-2" id="noTimeLimitCheck">
                            <input class="form-check-input" type="checkbox" name="wb_no_time_limit" value="1" id="wbNoTimeLimitChk"
                                   onchange="toggleTimeLimitSliders(this.checked)"
                                   <?= ($wallboxConfig['wb_no_time_limit'] ?? '0') === '1' ? 'checked' : '' ?>>
                            <label class="form-check-label text-muted small fw-bold" for="wbNoTimeLimitChk">
                                <i class="fas fa-infinity me-1 text-info"></i> Kein Zeitfenster &mdash; immer in den g&uuml;nstigsten Stunden laden (24h / Octopus)
                            </label>
                        </div>

                        <div id="timeLimitSliders" <?= ($wallboxConfig['wb_no_time_limit'] ?? '0') === '1' ? 'style="opacity:0.35; pointer-events:none;"' : '' ?>>
                        <div class="mb-3">
                            <label for="Wbvon" class="form-label text-muted small fw-bold d-flex justify-content-between mb-1">
                                <span>Startzeit (Wbvon)</span>
                                <span id="wbvonVal" class="badge <?= $showWbvonHint ? 'bg-warning text-dark' : 'bg-secondary text-white' ?>"><?= str_pad(htmlspecialchars($wbvonDisplayHour), 2, '0', STR_PAD_LEFT) ?>:00</span>
                            </label>
                            <input type="range" id="Wbvon" name="Wbvon" class="form-range"
                                   value="<?= htmlspecialchars($wbvonDisplayHour) ?>" min="0" max="23" step="1" oninput="document.getElementById('wbvonVal').innerText = String(this.value).padStart(2, '0') + ':00';">
                            <?php if ($showWbvonHint): ?>
                                <div class="text-warning small mt-1"><i class="fas fa-exclamation-triangle"></i> Wbvon (<?= htmlspecialchars($wallboxConfig['wbvon']) ?>) liegt in der Vergangenheit.</div>
                            <?php endif; ?>
                        </div>

                        <div class="mb-3">
                            <label for="Wbbis" class="form-label text-muted small fw-bold d-flex justify-content-between mb-1">
                                <span>Endzeit (Wbbis)</span>
                                <span id="wbbisVal" class="badge bg-secondary text-white"><?= str_pad(htmlspecialchars($wbbisDisplayHour), 2, '0', STR_PAD_LEFT) ?>:00</span>
                            </label>
                            <input type="range" id="Wbbis" name="Wbbis" class="form-range"
                                   value="<?= htmlspecialchars($wbbisDisplayHour) ?>" min="0" max="23" step="1" oninput="document.getElementById('wbbisVal').innerText = String(this.value).padStart(2, '0') + ':00';">
                        </div>
                        </div><!-- end timeLimitSliders -->

                        <script>
                        function toggleTimeLimitSliders(noLimit) {
                            var sliders = document.getElementById('timeLimitSliders');
                            if (noLimit) {
                                sliders.style.opacity = '0.35';
                                sliders.style.pointerEvents = 'none';
                            } else {
                                sliders.style.opacity = '';
                                sliders.style.pointerEvents = '';
                            }
                        }
                        </script>

                        <div class="form-check mb-4">
                            <input class="form-check-input" type="checkbox" name="adjust_wbvon" value="1" id="adjustCheck">
                            <label class="form-check-label text-muted small fw-bold" for="adjustCheck">
                                Wbvon setzen, falls Zeit &uuml;berschritten
                            </label>
                        </div>

                        <button type="submit" name="save_auto_settings" value="1" class="btn btn-outline-success w-100 rounded-pill fw-bold border-2 mt-auto">
                            ✓ Automatik speichern
                        </button>
                    </form>
                </div>
            </div>
        </div>
    </div>
    <?php endif; // Ende des alten Wallbox-Blocks ?>

    <div class="card shadow-sm mb-4" id="vehicle-assignment-card" style="border-radius: 16px;">
        <div class="card-body p-3">
            <div class="d-flex justify-content-between align-items-center mb-3 flex-wrap gap-2">
                <h6 class="card-title text-info fw-bold m-0"><i class="fas fa-car-side me-2"></i>Fahrzeugzuordnung</h6>
                <span class="badge bg-info-subtle text-info border border-info-subtle">WB1<?= $hasWb2 ? ' / WB2' : '' ?></span>
            </div>

            <form action="<?= htmlspecialchars($formAction) ?>" method="post" id="vehicleAssignmentForm" onsubmit="return false;">
                <div class="row g-2 mb-3">
                    <div class="col-12 col-md-4 col-xl-3">
                        <label class="form-label text-muted small fw-bold mb-1" title="Reserve im Hausspeicher für die Wallbox-Regelung. Unterhalb dieses SoC wird die Wallbox je nach Modus gehalten oder gesperrt.">Reserve im Hausspeicher</label>
                        <div class="input-group input-group-sm">
                            <input type="number" min="0" max="100" name="wbminsoc" class="form-control rounded-start-pill fw-bold"
                                   data-e3dc-floor="<?= htmlspecialchars($e3dcHouseReserveFloor !== null ? (string)$e3dcHouseReserveFloor : '') ?>"
                                   value="<?= htmlspecialchars($simpleHouseReserve) ?>">
                            <span class="input-group-text">%</span>
                            <button class="btn btn-secondary rounded-end-pill px-3" type="button" onclick="setWallboxHouseReserve('advanced')">Setzen</button>
                        </div>
                        <?php if ($houseReserveFloorNotice !== ''): ?>
                            <div class="form-text small text-warning">
                                <i class="fas fa-shield-alt me-1"></i><?= htmlspecialchars($houseReserveFloorNotice) ?>
                            </div>
                        <?php endif; ?>
                    </div>
                    <div class="col-12 col-md-8 col-xl-9 d-flex align-items-end">
                        <div class="small text-body-secondary pb-1">
                            Schützt den Hausspeicher, wenn PV knapp wird oder Netzladen begrenzt werden soll. Die Wallbox-Hardwarelimits stehen im Konfigurations-Editor und werden hier nur angezeigt.
                        </div>
                    </div>
                </div>
                <div class="<?= $hasWb2 ? 'row g-3 align-items-stretch' : 'row g-3' ?>">
                    <div class="<?= $hasWb2 ? 'col-12 col-xl-6' : 'col-12' ?>">
                        <div class="vehicle-assignment-panel border rounded-3 p-3 h-100 bg-body-tertiary">
                            <div class="d-flex justify-content-between align-items-center mb-3">
                                <h6 class="fw-bold mb-0 text-info"><i class="fas fa-plug me-2"></i>Wallbox 1 <span class="badge bg-info-subtle text-info border border-info-subtle ms-1">max <?= (int)$wb1MaxAmpLabel ?>A</span></h6>
                                <span class="badge bg-info-subtle text-info small">Haupteinfahrt</span>
                            </div>

                            <div class="row g-2 align-items-end">
                                <div class="col-12 col-lg-4">
                                    <label class="form-label text-muted small fw-bold mb-1">Fahrzeug</label>
                                    <select name="wb1_car_id" id="wb1_car_selector" class="form-select form-select-sm rounded-pill fw-bold car-selector" data-wb="1">
                                        <?php $wb1SelectedCar = canonicalWallboxVehicleSelection($wallboxConfig['wb1_car_id'] ?? '__none', $saved_cars); ?>
                                        <?php foreach ($vehicleSelectOptions as $vehicleOption): ?>
                                            <option value="<?= htmlspecialchars($vehicleOption['id']) ?>"
                                                    data-name="<?= htmlspecialchars($vehicleOption['name'] ?? '', ENT_QUOTES) ?>"
                                                    data-capacity="<?= htmlspecialchars((string)($vehicleOption['capacity'] ?? ''), ENT_QUOTES) ?>"
                                                    data-power="<?= htmlspecialchars((string)($vehicleOption['power'] ?? ''), ENT_QUOTES) ?>"
                                                    data-max-phases="<?= htmlspecialchars((string)($vehicleOption['max_phases'] ?? ''), ENT_QUOTES) ?>"
                                                    data-target="<?= htmlspecialchars((string)($vehicleOption['target_soc'] ?? ''), ENT_QUOTES) ?>"
                                                    data-max="<?= htmlspecialchars((string)($vehicleOption['max_soc'] ?? ''), ENT_QUOTES) ?>"
                                                    <?= ($wb1SelectedCar === $vehicleOption['id']) ? 'selected' : '' ?>>
                                                <?= htmlspecialchars($vehicleOption['label']) ?>
                                            </option>
                                        <?php endforeach; ?>
                                    </select>
                                </div>
                                <div class="col-6 col-lg-2">
                                    <label class="form-label text-muted small fw-bold mb-1">Akku kWh</label>
                                    <input type="number" step="0.1" name="wb1_capacity" class="form-control form-control-sm rounded-pill" value="<?= htmlspecialchars($wallboxConfig['wb1_capacity'] ?? '72.0') ?>">
                                </div>
                                <div class="col-6 col-lg-2">
                                    <label class="form-label text-muted small fw-bold mb-1">Leistung kW</label>
                                    <input type="number" step="0.1" name="wb1_charge_power" class="form-control form-control-sm rounded-pill" value="<?= htmlspecialchars($wallboxConfig['wb1_charge_power'] ?? '11.0') ?>">
                                </div>
                                <div class="col-6 col-lg-2">
                                    <label class="form-label text-muted small fw-bold mb-1">Ziel %</label>
                                    <input type="number" name="wb1_target_soc" class="form-control form-control-sm rounded-pill" value="<?= htmlspecialchars($wallboxConfig['wb1_target_soc'] ?? '80') ?>">
                                </div>
                                <div class="col-6 col-lg-2">
                                    <label class="form-label text-muted small fw-bold mb-1">Boost %</label>
                                    <input type="number" name="wb1_max_soc_si" class="form-control form-control-sm rounded-pill" value="<?= htmlspecialchars($wallboxConfig['wb1_max_soc_si'] ?? '90') ?>">
                                </div>
                            </div>

                            <div class="input-group input-group-sm mt-3">
                                <span class="input-group-text rounded-start-pill fw-bold">Start-SoC</span>
                                <input type="number" id="manual_soc_wb1" name="manual_soc_wb1" class="form-control" placeholder="IST %">
                                <button class="btn btn-secondary rounded-end-pill px-3" type="button" onclick="setManualSoC(1)">Setzen</button>
                            </div>
                        </div>
                    </div>

                    <?php if ($hasWb2): ?>
                    <div class="col-12 col-xl-6">
                        <div class="vehicle-assignment-panel border rounded-3 p-3 h-100 bg-body-tertiary">
                            <div class="d-flex justify-content-between align-items-center mb-3">
                                <h6 class="fw-bold mb-0 text-warning"><i class="fas fa-plug me-2"></i>Wallbox 2 <span class="badge bg-warning-subtle text-warning border border-warning-subtle ms-1">max <?= (int)$wb2MaxAmpLabel ?>A</span></h6>
                                <span class="badge bg-warning-subtle text-warning small">Garage</span>
                            </div>

                            <div class="row g-2 align-items-end">
                                <div class="col-12 col-lg-4">
                                    <label class="form-label text-muted small fw-bold mb-1">Fahrzeug</label>
                                    <select name="wb2_car_id" id="wb2_car_selector" class="form-select form-select-sm rounded-pill fw-bold car-selector" data-wb="2">
                                        <?php $wb2SelectedCar = canonicalWallboxVehicleSelection($wallboxConfig['wb2_car_id'] ?? '__none', $saved_cars); ?>
                                        <?php foreach ($vehicleSelectOptions as $vehicleOption): ?>
                                            <option value="<?= htmlspecialchars($vehicleOption['id']) ?>"
                                                    data-name="<?= htmlspecialchars($vehicleOption['name'] ?? '', ENT_QUOTES) ?>"
                                                    data-capacity="<?= htmlspecialchars((string)($vehicleOption['capacity'] ?? ''), ENT_QUOTES) ?>"
                                                    data-power="<?= htmlspecialchars((string)($vehicleOption['power'] ?? ''), ENT_QUOTES) ?>"
                                                    data-max-phases="<?= htmlspecialchars((string)($vehicleOption['max_phases'] ?? ''), ENT_QUOTES) ?>"
                                                    data-target="<?= htmlspecialchars((string)($vehicleOption['target_soc'] ?? ''), ENT_QUOTES) ?>"
                                                    data-max="<?= htmlspecialchars((string)($vehicleOption['max_soc'] ?? ''), ENT_QUOTES) ?>"
                                                    <?= ($wb2SelectedCar === $vehicleOption['id']) ? 'selected' : '' ?>>
                                                <?= htmlspecialchars($vehicleOption['label']) ?>
                                            </option>
                                        <?php endforeach; ?>
                                    </select>
                                </div>
                                <div class="col-6 col-lg-2">
                                    <label class="form-label text-muted small fw-bold mb-1">Akku kWh</label>
                                    <input type="number" step="0.1" name="wb2_capacity" class="form-control form-control-sm rounded-pill" value="<?= htmlspecialchars($wallboxConfig['wb2_capacity'] ?? '72.0') ?>">
                                </div>
                                <div class="col-6 col-lg-2">
                                    <label class="form-label text-muted small fw-bold mb-1">Leistung kW</label>
                                    <input type="number" step="0.1" name="wb2_charge_power" class="form-control form-control-sm rounded-pill" value="<?= htmlspecialchars($wallboxConfig['wb2_charge_power'] ?? '11.0') ?>">
                                </div>
                                <div class="col-6 col-lg-2">
                                    <label class="form-label text-muted small fw-bold mb-1">Ziel %</label>
                                    <input type="number" name="wb2_target_soc" class="form-control form-control-sm rounded-pill" value="<?= htmlspecialchars($wallboxConfig['wb2_target_soc'] ?? '80') ?>">
                                </div>
                                <div class="col-6 col-lg-2">
                                    <label class="form-label text-muted small fw-bold mb-1">Boost %</label>
                                    <input type="number" name="wb2_max_soc_si" class="form-control form-control-sm rounded-pill" value="<?= htmlspecialchars($wallboxConfig['wb2_max_soc_si'] ?? '90') ?>">
                                </div>
                            </div>

                            <div class="input-group input-group-sm mt-3">
                                <span class="input-group-text rounded-start-pill fw-bold">Start-SoC</span>
                                <input type="number" id="manual_soc_wb2" name="manual_soc_wb2" class="form-control" placeholder="IST %">
                                <button class="btn btn-secondary rounded-end-pill px-3" type="button" onclick="setManualSoC(2)">Setzen</button>
                            </div>
                        </div>
                    </div>
                    <?php endif; ?>
                </div>

                <div class="d-flex justify-content-between align-items-center gap-2 mt-3 flex-wrap">
                    <span class="small text-body-secondary">Fahrzeugauswahl und Profilwerte werden automatisch gespeichert.</span>
                    <span id="vehicleAssignmentStatus" class="badge bg-info-subtle text-info border border-info-subtle">Bereit</span>
                </div>
            </form>
        </div>
    </div>

    <div class="card shadow-sm mb-4" style="border-radius: 16px;">
        <div class="card-body p-3">
            <div class="d-flex justify-content-between align-items-center mb-2">
                <h6 class="card-title text-primary fw-bold m-0"><i class="fas fa-brain me-2"></i>Ladeplan &amp; Ziel-Laden</h6>
                <?php if ($isNativeEnabled && ($wallboxConfig['smart_wbhour_enable'] ?? '0') === '1' && $chargingSlots > 0): ?>
                    <span class="badge bg-success shadow-sm fs-6"><?= number_format(($chargingSlots * 15) / 60, 2, ',', '.') ?> h geplant</span>
                <?php endif; ?>
            </div>
            <?php if ($isNativeEnabled): ?>
                <p class="small text-muted mb-3">
                    Plant pro Wallbox eigene günstige Ladezeiten. Sofortladen wird direkt im Modus der jeweiligen Wallbox gewählt.
                </p>
            <?php else: ?>
                <p class="small text-muted mb-3">Berechnet die ben&ouml;tigte Ladezeit (`wbhour`) automatisch. Erfordert eine Fahrzeug-Integration.</p>
            <?php endif; ?>

            <?php
            $wb1SmartPlanActive = (($wallboxConfig['wb1_smart_wbhour_enable'] ?? $wallboxConfig['smart_wbhour_enable'] ?? '0') === '1');
            $wb2SmartPlanActive = (($wallboxConfig['wb2_smart_wbhour_enable'] ?? '0') === '1');
            $wb1NativeEcoActive = (($wallboxConfig['wb1_native_eco'] ?? $wallboxConfig['wb_native_eco'] ?? '0') === '1');
            $wb2NativeEcoActive = (($wallboxConfig['wb2_native_eco'] ?? '0') === '1');
            $wb1NativePlanHours = max(0, min(24, (int)($wallboxConfig['wb1_plan_hours'] ?? $wallboxConfig['wbhour'] ?? 0)));
            $wb2NativePlanHours = max(0, min(24, (int)($wallboxConfig['wb2_plan_hours'] ?? 0)));
            $activePlanLabels = [];
            foreach ([1, 2] as $activePlanWb) {
                if ($activePlanWb === 2 && !$hasWb2) continue;
                if (!empty($activePlanSlotsBeforePost[$activePlanWb]['active'])) {
                    $activePlanLabels[] = $activePlanSlotsBeforePost[$activePlanWb]['label'];
                }
            }
            ?>
            <?php if (!empty($activePlanLabels)): ?>
                <div class="alert alert-warning py-2 px-3 small mb-3" role="alert" data-active-plan-warning>
                    Aktives Ladefenster: <?= htmlspecialchars(implode(', ', $activePlanLabels)) ?>.
                    Verlängern im gleichen Zeitfenster läuft ohne Unterbrechung weiter; Start verschieben, Dauer 0 oder Plan löschen beendet die geplante Ladung.
                </div>
            <?php endif; ?>
            <form action="<?= htmlspecialchars($formAction) ?>" method="post" id="smartChargingForm">
                <?php
                $wallboxPriceLimit = (float)str_replace(',', '.', (string)($wallboxConfig['dvcarlimit'] ?? '0.0'));
                ?>
                <div class="row g-2 align-items-end mb-3">
                    <div class="col-12 col-md-4">
                        <label class="form-label text-muted small fw-bold mb-1" for="wallboxPriceLimit">
                            <i class="fas fa-coins me-1"></i>Netzpreislimit
                            <i class="fas fa-info-circle ms-1 text-info" data-bs-toggle="tooltip" data-bs-placement="top"
                               title="Gilt ausschließlich für den Modus &quot;Sofort bis Preislimit&quot; und spontanen Netzstrom. Geplante Ladefenster werden nicht gekürzt, nicht blockiert und nicht gelöscht."></i>
                        </label>
                        <div class="input-group input-group-sm">
                            <input type="number" step="0.1" min="0" max="200" inputmode="decimal" id="wallboxPriceLimit" name="dvcarlimit"
                                   class="form-control rounded-start-pill"
                                   value="<?= htmlspecialchars(number_format($wallboxPriceLimit, 1, '.', '')) ?>">
                            <span class="input-group-text rounded-end-pill">ct/kWh</span>
                        </div>
                        <div id="wallboxPriceLimitGuard" data-price-limit-guard class="form-text small mt-1" aria-live="polite"></div>
                    </div>
                    <div class="col-12 col-md-8 small text-muted">
                        Wirkt nur im Modus "Sofort bis Preislimit" für spontanen Netzstrom. Geplante Ladefenster bleiben gültig und wählen weiter die günstigsten Slots.
                    </div>
                </div>
                <div class="row mb-3 g-3 align-items-stretch">
                    <?php
                    $planPanels = [
                        1 => ['title' => 'Wallbox 1', 'color' => 'info', 'hours' => $wb1NativePlanHours, 'smart' => $wb1SmartPlanActive, 'eco' => $wb1NativeEcoActive, 'from' => $wb1PlanFrom, 'to' => $wb1PlanTo],
                        2 => ['title' => 'Wallbox 2', 'color' => 'warning', 'hours' => $wb2NativePlanHours, 'smart' => $wb2SmartPlanActive, 'eco' => $wb2NativeEcoActive, 'from' => $wb2PlanFrom, 'to' => $wb2PlanTo],
                    ];
                    foreach ($planPanels as $planWb => $planPanel):
                        if ($planWb === 2 && !$hasWb2) continue;
                        $planFromIsNow = in_array(strtolower(trim((string)$planPanel['from'])), ['now', 'jetzt'], true);
                        $planFromTimeValue = $planFromIsNow ? date('H:i') : $planPanel['from'];
                    ?>
                    <div class="<?= $hasWb2 ? 'col-12 col-xl-6' : 'col-12' ?>">
                        <div class="border rounded-3 p-3 h-100 bg-body-tertiary border-<?= $planPanel['color'] ?>-subtle">
                            <div class="d-flex justify-content-between align-items-center mb-3">
                                <h6 class="fw-bold text-<?= $planPanel['color'] ?> mb-0"><i class="fas fa-calendar-check me-2"></i><?= $planPanel['title'] ?></h6>
                                <span class="badge bg-<?= $planPanel['color'] ?>-subtle text-<?= $planPanel['color'] ?>">eigener Plan</span>
                            </div>
                            <?php if ($isNativeEnabled): ?>
                            <label for="nativePlanHoursWb<?= $planWb ?>" class="form-label text-muted small fw-bold d-flex justify-content-between mb-1">
                                <span data-bs-toggle="tooltip" title="Feste Anzahl günstiger Stunden. Bei aktivem Ziel-SoC berechnet E3DC-Control die Dauer automatisch aus Fahrzeug-SoC, Ziel-SoC und Ladeleistung.">Manuelle Ladezeit im Preisfenster</span>
                                <span id="nativePlanHoursValWb<?= $planWb ?>" class="badge bg-secondary text-white"><?= $planPanel['smart'] ? 'Auto' : ((int)$planPanel['hours'] . ' h') ?></span>
                            </label>
                            <input type="range" id="nativePlanHoursWb<?= $planWb ?>" name="native_plan_hours_wb<?= $planWb ?>" class="form-range"
                                   value="<?= (int)$planPanel['hours'] ?>" min="0" max="24" step="1"
                                   data-plan-hours-wb="<?= $planWb ?>"
                                   oninput="document.getElementById('nativePlanHoursValWb<?= $planWb ?>').innerText=this.value + ' h';">
                            <div class="row g-2 mt-1">
                                <div class="col-6">
                                    <label class="form-label text-muted small fw-bold mb-1" data-bs-toggle="tooltip" title="Startanker für die Optimierung. Jetzt bedeutet: ab dem aktuellen Zeitpunkt planen, auch nach einer späteren Neuberechnung."><i class="fas fa-play me-1"></i>Frühestens ab</label>
                                    <div class="input-group input-group-sm">
                                        <select name="save_wbvon_mode_wb<?= $planWb ?>"
                                                class="form-select form-select-sm rounded-start-pill"
                                                data-plan-from-mode-wb="<?= $planWb ?>"
                                                aria-label="Startmodus Wallbox <?= $planWb ?>">
                                            <option value="now" <?= $planFromIsNow ? 'selected' : '' ?>>Jetzt</option>
                                            <option value="time" <?= $planFromIsNow ? '' : 'selected' ?>>Uhrzeit</option>
                                        </select>
                                        <input type="time" name="save_wbvon_time_wb<?= $planWb ?>" class="form-control form-control-sm rounded-end-pill"
                                               data-plan-from-time-wb="<?= $planWb ?>"
                                               value="<?= htmlspecialchars($planFromTimeValue) ?>">
                                    </div>
                                </div>
                                <div class="col-6">
                                    <label class="form-label text-muted small fw-bold mb-1" data-bs-toggle="tooltip" title="Bis zu dieser Uhrzeit soll das geplante Laden fertig sein. Liegt die Uhrzeit schon vorbei, wird automatisch der nächste Tag genommen."><i class="fas fa-flag-checkered me-1"></i>Fertig bis</label>
                                    <input type="time" name="wbbis_time_wb<?= $planWb ?>" class="form-control form-control-sm rounded-pill"
                                           value="<?= htmlspecialchars($planPanel['to']) ?>">
                                </div>
                            </div>
                            <div class="d-flex gap-4 flex-wrap mt-3">
                                <div class="form-check form-switch">
                                    <input type="hidden" name="smart_wbhour_enable_wb<?= $planWb ?>" value="0">
                                    <input class="form-check-input" type="checkbox" name="smart_wbhour_enable_wb<?= $planWb ?>" value="1"
                                           id="smartWbhourEnableWb<?= $planWb ?>" data-plan-smart-wb="<?= $planWb ?>" <?= $planPanel['smart'] ? 'checked' : '' ?>>
                                    <label class="form-check-label small fw-bold" for="smartWbhourEnableWb<?= $planWb ?>" data-bs-toggle="tooltip" title="Nutzt den bekannten Fahrzeug-SoC. Wenn kein frischer SoC vorliegt, bleibt die manuelle Ladezeit massgeblich.">Ziel-SoC berechnet Dauer</label>
                                </div>
                                <div class="form-check form-switch">
                                    <input type="hidden" name="wb_native_eco_wb<?= $planWb ?>" value="0">
                                    <input class="form-check-input" type="checkbox" role="switch"
                                           id="nativeEcoToggleWb<?= $planWb ?>" name="wb_native_eco_wb<?= $planWb ?>" value="1"
                                           <?= $planPanel['eco'] ? 'checked' : '' ?>>
                                    <label class="form-check-label small fw-bold text-success" for="nativeEcoToggleWb<?= $planWb ?>" data-bs-toggle="tooltip" title="Berücksichtigt zusätzlich den Eco-/Netzdienlichkeits-Score. Der echte Tarifpreis bleibt die Hauptsortierung.">Eco</label>
                                </div>
                            </div>
                            <div class="form-text mt-2" style="font-size:0.72rem;">
                                Auto: Dauer kommt aus Fahrzeug-SoC, Ziel-SoC und Ladeleistung. Manuell: feste Anzahl günstiger Stunden.
                            </div>
                            <?php else: ?>
                            <div class="text-muted small">Native Wallbox-Steuerung ist nicht aktiv.</div>
                            <?php endif; ?>
                        </div>
                    </div>
                    <?php endforeach; ?>
                </div>

                <button type="submit" name="save_soc_settings" value="1" class="btn btn-outline-primary w-100 rounded-pill fw-bold border-2 mt-4">
                    <i class="fas fa-save me-1"></i> Ladeplan speichern
                </button>
            </form>
            <script>
            (function () {
                const activePlanSlots = <?= json_encode($activePlanSlotsBeforePost, JSON_UNESCAPED_SLASHES) ?>;
                function syncPlanHours(wb) {
                    const smart = document.querySelector('[data-plan-smart-wb="' + wb + '"]');
                    const range = document.querySelector('[data-plan-hours-wb="' + wb + '"]');
                    const badge = document.getElementById('nativePlanHoursValWb' + wb);
                    if (!smart || !range || !badge) return;
                    if (smart.checked) {
                        range.disabled = true;
                        range.classList.add('opacity-50');
                        badge.textContent = 'Auto';
                        badge.className = 'badge bg-primary text-white';
                    } else {
                        range.disabled = false;
                        range.classList.remove('opacity-50');
                        badge.textContent = range.value + ' h';
                        badge.className = 'badge bg-secondary text-white';
                    }
                }
                document.querySelectorAll('[data-plan-smart-wb]').forEach(function (el) {
                    const wb = el.getAttribute('data-plan-smart-wb');
                    el.addEventListener('change', function () { syncPlanHours(wb); });
                    syncPlanHours(wb);
                });
                function syncPlanNow(wb) {
                    const mode = document.querySelector('[data-plan-from-mode-wb="' + wb + '"]');
                    const time = document.querySelector('[data-plan-from-time-wb="' + wb + '"]');
                    if (!mode || !time) return;
                    const isNow = mode.value === 'now';
                    time.disabled = isNow;
                    time.classList.toggle('opacity-50', isNow);
                }
                document.querySelectorAll('[data-plan-from-mode-wb]').forEach(function (el) {
                    const wb = el.getAttribute('data-plan-from-mode-wb');
                    el.addEventListener('change', function () { syncPlanNow(wb); });
                    syncPlanNow(wb);
                });
                function parsePlanTime(value, fallbackHours, fallbackMinutes) {
                    const match = String(value || '').match(/^(\d{1,2}):(\d{2})$/);
                    if (!match) return { h: fallbackHours, m: fallbackMinutes };
                    return {
                        h: Math.max(0, Math.min(23, parseInt(match[1], 10))),
                        m: Math.max(0, Math.min(59, parseInt(match[2], 10)))
                    };
                }
                function postedPlanCoversActiveSlot(wb, active) {
                    const range = document.querySelector('[data-plan-hours-wb="' + wb + '"]');
                    const smart = document.querySelector('[data-plan-smart-wb="' + wb + '"]');
                    const fromMode = document.querySelector('[data-plan-from-mode-wb="' + wb + '"]');
                    const fromTime = document.querySelector('[data-plan-from-time-wb="' + wb + '"]');
                    const toTime = document.querySelector('[name="wbbis_time_wb' + wb + '"]');
                    if (!range || !fromMode || !toTime || !active || !active.active) return true;
                    const manualHours = parseInt(range.value || '0', 10) || 0;
                    if (!(smart && smart.checked) && manualHours <= 0) return false;
                    const slotStart = new Date((parseInt(active.slot_ts, 10) || 0) * 1000);
                    const slotMs = slotStart.getTime();
                    const from = new Date(slotMs);
                    const to = new Date(slotMs);
                    if (fromMode.value === 'now') {
                        from.setTime(Date.now() - 15 * 60 * 1000);
                    } else {
                        const hm = parsePlanTime(fromTime ? fromTime.value : '', 0, 0);
                        from.setHours(hm.h, hm.m, 0, 0);
                    }
                    const endHm = parsePlanTime(toTime.value, 7, 0);
                    to.setHours(endHm.h, endHm.m, 0, 0);
                    if (fromMode.value !== 'now' && from.getTime() > to.getTime()) {
                        if (slotMs < to.getTime()) {
                            from.setDate(from.getDate() - 1);
                        } else {
                            to.setDate(to.getDate() + 1);
                        }
                    } else if (fromMode.value === 'now' && slotMs >= to.getTime()) {
                        to.setDate(to.getDate() + 1);
                    }
                    return from.getTime() <= slotMs && slotMs < to.getTime();
                }
                const smartForm = document.getElementById('smartChargingForm');
                if (smartForm) {
                    smartForm.addEventListener('submit', function (event) {
                        const interrupted = [];
                        Object.keys(activePlanSlots || {}).forEach(function (wb) {
                            const active = activePlanSlots[wb];
                            if (active && active.active && !postedPlanCoversActiveSlot(wb, active)) {
                                interrupted.push(active.label || ('WB' + wb));
                            }
                        });
                        if (!interrupted.length) return;
                        const text = 'Die aktuelle Planladung wird durch diese Änderung unterbrochen: ' +
                            interrupted.join(', ') +
                            '. Fortfahren? Not-Aus und Plan löschen bleiben davon unberührt.';
                        if (!window.confirm(text)) {
                            event.preventDefault();
                        }
                    });
                }
            })();
            </script>
        </div>
    </div>

    <?php if (!empty($observed_openwb_profiles)): ?>
    <div class="card shadow-sm mb-4 border-info" style="border-radius: 16px;">
        <div class="card-body p-3">
            <h6 class="card-title text-info fw-bold mb-2"><i class="fas fa-id-badge me-2"></i>openWB-Ladeprofil (beobachtet)</h6>
            <p class="small text-muted mb-3">Das Ladeprofil ist eine Live-Beobachtung aus openWB. Es ändert weder ein gespeichertes Fahrzeug noch die statische E3DC-Zuordnung.</p>
            <?php foreach ($observed_openwb_profiles as $profile): ?>
                <div class="d-flex flex-wrap gap-2 align-items-center mb-2 small">
                    <span class="badge text-bg-info">WB<?= (int)$profile['wb'] ?></span>
                    <strong>Ladeprofil: <?= htmlspecialchars($profile['name']) ?></strong>
                    <span class="text-muted">Live-Fahrzeug: <?= htmlspecialchars($profile['vehicle_name'] !== '' ? $profile['vehicle_name'] : 'nicht namentlich erkannt') ?></span>
                    <span class="text-muted">Aktuell stabile Kennung: <?= htmlspecialchars(!empty($profile['stable_identity_current']) && $profile['vehicle_id'] !== '' ? $profile['vehicle_id'] : 'nicht geliefert') ?></span>
                    <?php if (!empty($profile['retained_identity_present'])): ?><span class="text-warning">Alte Sitzungskennung nur diagnostisch erhalten</span><?php endif; ?>
                </div>
            <?php endforeach; ?>
        </div>
    </div>
    <?php endif; ?>

    <?php if (!empty($unknown_openwb_vehicles)): ?>
    <div class="card shadow-sm mb-4 border-warning" style="border-radius: 16px;">
        <div class="card-body p-3">
            <h6 class="card-title text-warning fw-bold mb-2"><i class="fas fa-user-plus me-2"></i>Unbekanntes Fahrzeug erkannt</h6>
            <p class="small text-muted mb-3">Die Wallbox liefert eine Fahrzeugkennung, die noch keiner Vorlage zugeordnet ist. Du kannst sie neu anlegen oder mit einem vorhandenen Bluelink-/Cloud-Fahrzeug verknüpfen.</p>
            <?php foreach ($unknown_openwb_vehicles as $det): ?>
                <?php
                    $detWb = (int)($det['wb'] ?? 1);
                    $detName = trim((string)($det['name'] ?? ''));
                    $detVehicleId = trim((string)($det['vehicle_id'] ?? ''));
                    $detSoc = is_numeric($det['soc'] ?? null) ? (float)$det['soc'] : '';
                    $detCapacity = (float)($det['capacity'] ?? 0);
                    $detPower = (float)($det['power'] ?? 11.0);
                    $suggestName = $detName !== '' ? $detName : ('Fahrzeug WB' . $detWb);
                    $defaultCloudName = '';
                    if (count($live_cloud_vehicles) === 1) {
                        $onlyCloud = $live_cloud_vehicles[0];
                        $cloudName = trim((string)($onlyCloud['name'] ?? ''));
                        if ($cloudName !== '') {
                            $suggestName = $cloudName;
                            $defaultCloudName = $cloudName;
                        }
                    }
                ?>
                <form action="<?= htmlspecialchars($formAction) ?>" method="post" class="p-3 mb-3 rounded-3 border border-warning border-opacity-25" style="background: rgba(255, 193, 7, 0.06);">
                    <input type="hidden" name="save_custom_car" value="1">
                    <input type="hidden" name="custom_car_assign_wb" value="<?= $detWb ?>">
                    <input type="hidden" name="custom_car_current_soc" value="<?= htmlspecialchars((string)$detSoc) ?>">
                    <input type="hidden" name="custom_car_cloud_vehicle_name" value="<?= htmlspecialchars($defaultCloudName) ?>" data-cloud-name-hidden="unknown-<?= $detWb ?>">
                    <div class="d-flex justify-content-between gap-2 flex-wrap mb-3">
                        <div>
                            <strong class="text-body">WB<?= $detWb ?>: <?= htmlspecialchars($det['source'] ?? 'openWB') ?></strong>
                            <div class="small text-muted">
                                Kennung: <code><?= htmlspecialchars($detVehicleId !== '' ? $detVehicleId : 'ohne Kennung') ?></code>
                                <?php if ($detSoc !== ''): ?> · SoC: <?= number_format((float)$detSoc, 1, ',', '.') ?>%<?php endif; ?>
                            </div>
                        </div>
                        <span class="badge text-bg-warning align-self-start">Zuordnung offen</span>
                    </div>
                    <div class="row g-2">
                        <div class="col-12 col-lg-4">
                            <label class="form-label text-muted small fw-bold mb-1">Mit vorhandener Vorlage verknüpfen</label>
                            <select name="custom_car_link_saved_id" class="form-select form-select-sm rounded-pill">
                                <option value="">Neue Vorlage anlegen</option>
                                <?php foreach ($saved_cars as $car): ?>
                                    <option value="<?= htmlspecialchars($car['id'] ?? '') ?>"><?= htmlspecialchars($car['name'] ?? 'Fahrzeug') ?></option>
                                <?php endforeach; ?>
                            </select>
                        </div>
                        <div class="col-12 col-lg-4">
                            <label class="form-label text-muted small fw-bold mb-1">Bluelink-/Cloud-Fahrzeug</label>
                            <select name="custom_car_cloud_vehicle_id" class="form-select form-select-sm rounded-pill" data-cloud-select data-cloud-hidden="unknown-<?= $detWb ?>">
                                <option value="">Keine Cloud-Verknüpfung</option>
                                <?php foreach ($live_cloud_vehicles as $cloud): ?>
                                    <?php
                                        $cloudId = (string)($cloud['id'] ?? $cloud['vin'] ?? '');
                                        $cloudName = (string)($cloud['name'] ?? $cloudId);
                                        $selected = (count($live_cloud_vehicles) === 1) ? 'selected' : '';
                                    ?>
                                    <option value="<?= htmlspecialchars($cloudId) ?>" data-name="<?= htmlspecialchars($cloudName) ?>" <?= $selected ?>><?= htmlspecialchars($cloudName) ?></option>
                                <?php endforeach; ?>
                            </select>
                        </div>
                        <div class="col-12 col-lg-4">
                            <label class="form-label text-muted small fw-bold mb-1">Name</label>
                            <input type="text" name="custom_car_name" class="form-control form-control-sm rounded-pill" value="<?= htmlspecialchars($suggestName) ?>" required>
                        </div>
                        <div class="col-12 col-md-5">
                            <label class="form-label text-muted small fw-bold mb-1">Fahrzeug-ID / MAC / RFID</label>
                            <input type="text" name="custom_car_vehicle_id" class="form-control form-control-sm rounded-pill" value="<?= htmlspecialchars($detVehicleId) ?>">
                        </div>
                        <div class="col-6 col-md-2">
                            <label class="form-label text-muted small fw-bold mb-1">Akku (kWh)</label>
                            <input type="number" step="0.1" name="custom_car_capacity" class="form-control form-control-sm rounded-pill" value="<?= htmlspecialchars((string)($detCapacity > 0 ? $detCapacity : 72.0)) ?>">
                        </div>
                        <div class="col-6 col-md-2">
                            <label class="form-label text-muted small fw-bold mb-1">Lader (kW)</label>
                            <input type="number" step="0.1" name="custom_car_power" class="form-control form-control-sm rounded-pill" value="<?= htmlspecialchars((string)($detPower > 0 ? $detPower : 11.0)) ?>">
                        </div>
                        <div class="col-6 col-md-1">
                            <label class="form-label text-muted small fw-bold mb-1">Ph.</label>
                            <select name="custom_car_max_phases" class="form-select form-select-sm rounded-pill">
                                <?php $detPhases = wallboxVehicleMaxPhases(['power' => ($detPower > 0 ? $detPower : 11.0), 'max_phases' => $det['max_phases'] ?? null]); ?>
                                <option value="1" <?= (int)$detPhases === 1 ? 'selected' : '' ?>>1p</option>
                                <option value="2" <?= (int)$detPhases === 2 ? 'selected' : '' ?>>2p</option>
                                <option value="3" <?= (int)$detPhases === 3 || !in_array((int)$detPhases, [1, 2, 3], true) ? 'selected' : '' ?>>3p</option>
                            </select>
                        </div>
                        <div class="col-6 col-md-1">
                            <label class="form-label text-muted small fw-bold mb-1">Ziel</label>
                            <input type="number" name="custom_car_target" class="form-control form-control-sm rounded-pill" value="80">
                        </div>
                        <div class="col-6 col-md-1">
                            <label class="form-label text-muted small fw-bold mb-1">Max</label>
                            <input type="number" name="custom_car_max" class="form-control form-control-sm rounded-pill" value="90">
                        </div>
                        <div class="col-6 col-md-1">
                            <label class="form-label text-muted small fw-bold mb-1">Eff.</label>
                            <input type="number" name="custom_car_efficiency" class="form-control form-control-sm rounded-pill" value="90">
                        </div>
                        <div class="col-6 col-md-2">
                            <label class="form-label text-muted small fw-bold mb-1">kWh/100 km</label>
                            <input type="number" step="0.1" name="custom_car_consumption" class="form-control form-control-sm rounded-pill" value="18">
                        </div>
                        <div class="col-12 col-md-4 d-flex align-items-end">
                            <button type="submit" class="btn btn-warning btn-sm rounded-pill fw-bold w-100">
                                <i class="fas fa-link me-1"></i> Fahrzeug übernehmen
                            </button>
                        </div>
                    </div>
                </form>
            <?php endforeach; ?>
        </div>
    </div>
    <?php endif; ?>

    <div class="row g-4 mb-4">
        <!-- NEU: Eigene Fahrzeug-Vorlagen verwalten -->
        <div class="col-12 col-lg-6">
            <div class="card shadow-sm h-100" style="border-radius: 16px;">
                <div class="card-body p-3">
                    <h6 class="card-title text-success fw-bold mb-3"><i class="fas fa-car me-2"></i>Eigene Fahrzeug-Vorlagen</h6>
                    <p class="small text-muted mb-3">Speichere eigene Fahrzeuge ohne Cloud-Anbindung als feste Vorlage für die Dropdowns ab.</p>

                    <form action="<?= htmlspecialchars($formAction) ?>" method="post" class="mb-4">
                        <input type="hidden" name="custom_car_cloud_vehicle_name" value="" data-cloud-name-hidden="manual">
                        <div class="row g-2 mb-3">
                            <div class="col-12 col-md-4">
                                <label class="form-label text-muted small fw-bold mb-1">Name / Modell</label>
                                <input type="text" name="custom_car_name" class="form-control form-control-sm rounded-pill" placeholder="z.B. BMW iX3" required>
                            </div>
                            <div class="col-6 col-md-2">
                                <label class="form-label text-muted small fw-bold mb-1">Akku (kWh)</label>
                                <input type="number" step="0.1" name="custom_car_capacity" class="form-control form-control-sm rounded-pill" value="72.0" required>
                            </div>
                            <div class="col-6 col-md-2">
                                <label class="form-label text-muted small fw-bold mb-1">Lader (kW)</label>
                                <input type="number" step="0.1" name="custom_car_power" class="form-control form-control-sm rounded-pill" value="11.0" required>
                            </div>
                            <div class="col-6 col-md-2">
                                <label class="form-label text-muted small fw-bold mb-1">Max. Phasen</label>
                                <select name="custom_car_max_phases" class="form-select form-select-sm rounded-pill">
                                    <option value="1">1p</option>
                                    <option value="2">2p</option>
                                    <option value="3" selected>3p</option>
                                </select>
                            </div>
                            <div class="col-6 col-md-2">
                                <label class="form-label text-muted small fw-bold mb-1">Ziel-SoC</label>
                                <input type="number" name="custom_car_target" class="form-control form-control-sm rounded-pill" value="80" required>
                            </div>
                            <div class="col-6 col-md-2">
                                <label class="form-label text-muted small fw-bold mb-1">Max-SoC</label>
                                <input type="number" name="custom_car_max" class="form-control form-control-sm rounded-pill" value="90" required>
                            </div>
                        </div>
                        <div class="row g-2 mb-3">
                            <div class="col-12 col-md-6">
                                <label class="form-label text-muted small fw-bold mb-1">Fahrzeug-ID / MAC (openWB Pro)</label>
                                <input type="text" name="custom_car_vehicle_id" class="form-control form-control-sm rounded-pill" placeholder="z.B. 02:00:00:00:00:01">
                                <div class="form-text small text-muted">Optional: Wird die Pro ohne openWB-Software genutzt, kann der Start-SoC diesem Fahrzeug zugeordnet werden.</div>
                            </div>
                            <div class="col-12 col-md-6">
                                <label class="form-label text-muted small fw-bold mb-1">Bluelink-/Cloud-Verknüpfung</label>
                                <select name="custom_car_cloud_vehicle_id" class="form-select form-select-sm rounded-pill" data-cloud-select data-cloud-hidden="manual">
                                    <option value="">Keine Cloud-Verknüpfung</option>
                                    <?php foreach ($live_cloud_vehicles as $cloud): ?>
                                        <?php
                                            $cloudId = (string)($cloud['id'] ?? $cloud['vin'] ?? '');
                                            $cloudName = (string)($cloud['name'] ?? $cloudId);
                                        ?>
                                        <option value="<?= htmlspecialchars($cloudId) ?>" data-name="<?= htmlspecialchars($cloudName) ?>"><?= htmlspecialchars($cloudName) ?></option>
                                    <?php endforeach; ?>
                                </select>
                                <div class="form-text small text-muted">Verhindert Doppelanzeigen, wenn dasselbe Auto über Wallbox und Bluelink auftaucht.</div>
                            </div>
                        </div>
                        <div class="row g-2 mb-3">
                            <div class="col-6 col-md-3">
                                <label class="form-label text-muted small fw-bold mb-1">Lade-Wirkungsgrad (%)</label>
                                <input type="number" step="1" min="50" max="100" name="custom_car_efficiency" class="form-control form-control-sm rounded-pill" value="90">
                            </div>
                            <div class="col-6 col-md-3">
                                <label class="form-label text-muted small fw-bold mb-1">Verbrauch (kWh/100 km)</label>
                                <input type="number" step="0.1" min="1" name="custom_car_consumption" class="form-control form-control-sm rounded-pill" value="18">
                            </div>
                        </div>
                        <button type="submit" name="save_custom_car" value="1" class="btn btn-outline-success btn-sm w-100 rounded-pill fw-bold border-2">
                            <i class="fas fa-plus"></i> Als Vorlage speichern
                        </button>
                    </form>

                    <?php if (!empty($saved_cars)): ?>
                    <div class="table-responsive">
                        <table class="table table-sm table-hover align-middle mb-0 vehicle-template-table" style="font-size: 0.85rem; border-radius: 8px; overflow: hidden;">
                            <thead>
                                <tr>
                                    <th class="ps-3 border-secondary-subtle">Fahrzeug</th>
                                    <th class="border-secondary-subtle">Akku</th>
                                    <th class="border-secondary-subtle">Leistung</th>
                                    <th class="border-secondary-subtle">Phasen</th>
                                    <th class="border-secondary-subtle">ID / MAC</th>
                                    <th class="text-end pe-3 border-secondary-subtle">Aktion</th>
                                </tr>
                            </thead>
                            <tbody>
                                <?php foreach ($saved_cars as $car): ?>
                                <tr>
                                    <td class="ps-3 border-secondary-subtle"><strong><?= htmlspecialchars($car['name'] ?? '') ?></strong></td>
                                    <td class="border-secondary-subtle"><?= number_format((float)($car['capacity'] ?? 0), 1, ',', '.') ?> kWh</td>
                                    <td class="border-secondary-subtle"><?= number_format((float)($car['power'] ?? 0), 1, ',', '.') ?> kW</td>
                                    <td class="border-secondary-subtle"><?= htmlspecialchars((string)(wallboxVehicleMaxPhases($car) ?: '--')) ?>p</td>
                                    <td class="border-secondary-subtle"><code class="small"><?= htmlspecialchars($car['vehicle_id'] ?? '') ?></code></td>
                                    <td class="text-end pe-3 border-secondary-subtle">
                                        <form action="<?= htmlspecialchars($formAction) ?>" method="post" style="display:inline;">
                                            <input type="hidden" name="delete_custom_car" value="<?= htmlspecialchars($car['id']) ?>">
                                            <button type="submit" class="btn btn-outline-danger btn-sm rounded-circle py-0 px-2" data-bs-toggle="tooltip" title="Löschen"><i class="fas fa-trash-alt" style="font-size: 0.7rem;"></i></button>
                                        </form>
                                    </td>
                                </tr>
                                <?php endforeach; ?>
                            </tbody>
                        </table>
                    </div>
                    <?php endif; ?>
                </div>
            </div>
        </div>

        <!-- NEU: Cloud Integration (Bluelink) -->
        <div class="col-12 col-lg-6">
            <div class="card shadow-sm h-100" style="border-radius: 16px;">
                <div class="card-body p-3">
                    <h6 class="card-title text-info fw-bold mb-3"><i class="fas fa-cloud me-2"></i>Fahrzeug Cloud-Integration (Auto SoC)</h6>
                    <p class="small text-muted mb-3">Verbinde dein Fahrzeug (Hyundai / Kia Bluelink), um den Echtzeit-Ladezustand (SoC) und Batterie-Infos automatisch abzurufen.</p>

                    <form action="<?= htmlspecialchars($formAction) ?>" method="post">
                        <div class="row g-3 mb-3">
                            <div class="col-12">
                                <label class="form-label text-muted small fw-bold mb-1">Token (Refresh Token)</label>
                                <input type="password" name="bluelink_refresh_token" class="form-control form-control-sm rounded-pill"
                                       value="<?= htmlspecialchars($wallboxConfig['bluelink_refresh_token'] ?? '') ?>">
                            </div>
                            <div class="col-12 col-md-6">
                                <label class="form-label text-muted small fw-bold mb-1">VIN (Fahrgestellnummer)</label>
                                <input type="text" name="bluelink_vin" class="form-control form-control-sm rounded-pill"
                                       value="<?= htmlspecialchars($wallboxConfig['bluelink_vin'] ?? '') ?>">
                            </div>
                            <div class="col-12 col-md-6">
                                <label class="form-label text-muted small fw-bold mb-1">Anzeigename</label>
                                <input type="text" name="bluelink_car_name" class="form-control form-control-sm rounded-pill"
                                       value="<?= htmlspecialchars($wallboxConfig['bluelink_car_name'] ?? '') ?>" placeholder="z.B. Hyundai IONIQ 5">
                            </div>
                            <div class="col-12 col-md-6">
                                <label class="form-label text-muted small fw-bold mb-1">Abfrage-Intervall (Min)</label>
                                <input type="number" name="bluelink_interval" class="form-control form-control-sm rounded-pill"
                                       value="<?= htmlspecialchars($wallboxConfig['bluelink_interval'] ?? '15') ?>">
                            </div>
                            <div class="col-12 col-md-6 mt-md-4 pt-md-3">
                                <div class="form-check form-switch ps-5">
                                    <input type="hidden" name="bluelink_ignore_plug_status" value="0">
                                    <input class="form-check-input" type="checkbox" name="bluelink_ignore_plug_status" value="1" id="blue_plug"
                                           <?= (isset($wallboxConfig['bluelink_ignore_plug_status']) && $wallboxConfig['bluelink_ignore_plug_status'] == '1') ? 'checked' : '' ?> style="transform: scale(1.2); margin-left: -2.5em;">
                                    <label class="form-check-label ms-1 text-muted small fw-bold" for="blue_plug" title="Immer abfragen, auch wenn das Auto laut API nicht eingesteckt ist.">
                                        Plug-Status ignorieren
                                    </label>
                                </div>
                            </div>
                        </div>
                        <button type="submit" name="save_cloud_integration" value="1" class="btn btn-outline-info btn-sm w-100 rounded-pill fw-bold border-2">
                            <i class="fas fa-save me-1"></i> Cloud-Daten speichern
                        </button>
                    </form>
                </div>
            </div>
        </div>
    </div>

    <script>
    document.querySelectorAll('[data-cloud-select]').forEach(function (select) {
        if (select.dataset.cloudSyncBound === '1') return;
        select.dataset.cloudSyncBound = '1';
        function syncCloudName() {
            const hiddenKey = select.getAttribute('data-cloud-hidden');
            const hidden = document.querySelector('[data-cloud-name-hidden="' + hiddenKey + '"]');
            if (!hidden) return;
            const option = select.options[select.selectedIndex];
            hidden.value = option ? (option.getAttribute('data-name') || '') : '';
        }
        select.addEventListener('change', syncCloudName);
        syncCloudName();
    });
    </script>

    </div>

</div>

<!-- Overlay History -->
<div id="wbHistoryOverlay" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; z-index:9999; background:rgba(0,0,0,0.85); align-items:center; justify-content:center; padding: 15px;">
    <div class="card shadow" style="width: 100%; max-width: 650px; max-height: 90vh; border-radius: 16px; display: flex; flex-direction: column; overflow: hidden; background-color: var(--bs-body-bg);">
        <div class="card-header d-flex justify-content-between align-items-center py-3 bg-transparent border-bottom border-secondary-subtle">
            <h5 class="m-0 fw-bold"><i class="fas fa-history me-2 text-info"></i>Historische Ladezeiten</h5>
            <button type="button" class="btn-close" onclick="document.getElementById('wbHistoryOverlay').style.display='none'"></button>
        </div>
        <div class="card-body overflow-auto p-0">
            <?php if (!empty($wbSessions)): ?>
                <div class="p-3 border-bottom border-secondary-subtle d-flex justify-content-between align-items-center" style="background-color: var(--bs-tertiary-bg);">
                    <div>
                        <small class="text-muted fw-bold text-uppercase" style="letter-spacing: 0.5px;">Gesamte Ladezeit</small>
                        <div class="fw-bold fs-5 text-body"><?= $totalHistoryHours ?> Std <?= $totalHistoryMinutes ?> Min</div>
                    </div>
                    <div class="text-end">
                        <small class="text-muted fw-bold text-uppercase" style="letter-spacing: 0.5px;">Gesamte Energie</small>
                        <div class="fw-bold fs-5 text-warning"><?= number_format($totalHistoryKwh, 2, ',', '.') ?> kWh</div>
                    </div>
                </div>
                <div class="table-responsive">
                    <table class="table mb-0 align-middle" style="font-size: 0.85rem;">
                        <tbody>
                            <?php
                            $lastDate = '';
                            $bgClass = '';
                            foreach ($wbSessions as $s):
                                $date = date('d.m.Y', $s['tsStart']);
                                $timeSpan = date('H:i', $s['tsStart']) . ' - ' . date('H:i', $s['tsEnd']);
                                $durationMins = round(($s['tsEnd'] - $s['tsStart']) / 60);
                                $durH = floor($durationMins / 60);
                                $durM = $durationMins % 60;
                                $durationStr = ($durH > 0 ? $durH . 'h ' : '') . sprintf('%02d', $durM) . 'm';

                                // Tagesschnitt zur Schätzung des Wallbox-Mix
                                $dayAutarky = 100;
                                if (isset($dailyDbData[$date])) {
                                    $dayAutarky = (float)$dailyDbData[$date]['autarky'];
                                    if ($dayAutarky < 0) $dayAutarky = 0;
                                    if ($dayAutarky > 100) $dayAutarky = 100;
                                }

                                $pvShareKwh = $s['kwh'] * ($dayAutarky / 100);
                                $gridShareKwh = $s['kwh'] - $pvShareKwh;

                                if ($date !== $lastDate) {
                                    $bgClass = ($bgClass === '') ? 'table-active' : '';
                                    echo '<tr class="'.$bgClass.' border-top border-2 border-secondary-subtle">';
                                    echo '<td colspan="3" class="fw-bold text-body py-2 ps-3"><div class="d-flex justify-content-between align-items-center"><div><i class="fas fa-calendar-day me-2 text-muted"></i>'.$date.'</div><div class="text-end" style="font-size:0.75rem;"><span class="badge bg-secondary opacity-75">Tagesschnitt-Mix: '.round($dayAutarky).'% PV/Bat</span></div></div></td>';
                                    echo '</tr>';
                                    $lastDate = $date;
                                }
                            ?>
                            <tr class="<?= $bgClass ?>">
                                <td class="ps-3 text-nowrap text-secondary fw-medium" style="width: 110px;">
                                    <?= $timeSpan ?>
                                </td>
                                <td class="text-muted text-nowrap">
                                    <i class="fas fa-stopwatch me-1 opacity-50"></i><?= $durationStr ?>
                                </td>
                                <td class="text-end pe-4">
                                    <div class="d-flex justify-content-end align-items-center flex-wrap gap-3">
                                        <div style="font-size: 0.8rem; letter-spacing: 0.3px;">
                                            <span class="text-success" title="PV- & Batterie-Anteil"><i class="fas fa-sun me-1 opacity-75"></i><?= number_format($pvShareKwh, 2, ',', '.') ?></span>
                                            <span class="text-muted mx-1 opacity-25">|</span>
                                            <span class="text-danger" title="Netz-Anteil"><i class="fas fa-bolt me-1 opacity-75"></i><?= number_format($gridShareKwh, 2, ',', '.') ?></span>
                                        </div>
                                        <div class="fw-bold text-warning text-end" style="font-size: 0.95rem; min-width: 80px;">
                                            +<?= number_format($s['kwh'], 2, ',', '.') ?> kWh
                                        </div>
                                    </div>
                                </td>
                            </tr>
                            <?php endforeach; ?>
                        </tbody>
                    </table>
                </div>
            <?php else: ?>
                <div class="p-5 text-center text-muted col-12 d-flex flex-column align-items-center">
                    <i class="fas fa-inbox fs-1 mb-3 opacity-50"></i>
                    <span>Keine Wallbox Ladehistorie vorhanden.</span>
                </div>
            <?php endif; ?>
        </div>
        <div class="card-footer bg-transparent py-3 text-center border-top border-secondary-subtle">
            <button type="button" class="btn btn-outline-secondary rounded-pill px-5 fw-bold" onclick="document.getElementById('wbHistoryOverlay').style.display='none'">Zurück</button>
        </div>
    </div>
</div>

<script>
const WALLBOX_AJAX_ENDPOINT = 'Wallbox.php';

function initWallboxViewToggle() {
    const panels = {
        simple: document.querySelector('[data-wallbox-view-panel="simple"]'),
        advanced: document.querySelector('[data-wallbox-view-panel="advanced"]')
    };
    const buttons = Array.from(document.querySelectorAll('[data-wallbox-view-toggle]'));
    if (!panels.simple || !panels.advanced || buttons.length === 0) return;

    function setView(view) {
        const nextView = view === 'advanced' ? 'advanced' : 'simple';
        document.documentElement.setAttribute('data-e3dc-wallbox-view', nextView);
        panels.simple.hidden = nextView !== 'simple';
        panels.advanced.hidden = nextView !== 'advanced';
        buttons.forEach(button => {
            const active = button.getAttribute('data-wallbox-view-toggle') === nextView;
            button.setAttribute('aria-pressed', active ? 'true' : 'false');
            button.classList.remove('btn-info', 'btn-secondary', 'btn-outline-secondary', 'btn-outline-info');
            if (active) {
                button.classList.add(nextView === 'simple' ? 'btn-info' : 'btn-secondary');
            } else {
                button.classList.add('btn-outline-secondary');
            }
        });
        try { window.localStorage.setItem('e3dc.wallbox.view', nextView); } catch (err) {}
    }

    buttons.forEach(button => {
        button.addEventListener('click', function () {
            setView(this.getAttribute('data-wallbox-view-toggle'));
        });
    });

    let storedView = 'simple';
    try { storedView = window.localStorage.getItem('e3dc.wallbox.view') || 'simple'; } catch (err) {}
    setView(storedView);
}

function initSimpleWallboxTargetControls() {
    const forms = Array.from(document.querySelectorAll('.wallbox-simple-card'));
    const fmt = value => String(value ?? '').trim();
    const globalReserve = document.querySelector('[data-simple-global-reserve]');
    const globalPrice = document.querySelector('[data-simple-global-price]');
    let globalSaveTimer = null;

    function selectedUnit(form) {
        return form.querySelector('[name="simple_target_unit"]:checked')?.value === 'kwh' ? 'kwh' : 'soc';
    }

    function selectedEnergy(form) {
        return form.querySelector('[name="simple_energy_mode"]:checked')?.value || 'pv';
    }

    function selectedIntent(form) {
        return form.querySelector('[name="simple_charge_intent"]:checked')?.value || 'surplus';
    }

    function clampHouseReserveValue(value, floorSource = null, applyFloor = true) {
        const raw = Number(String(value ?? '').replace(',', '.'));
        let next = Number.isFinite(raw) ? Math.max(0, Math.min(100, raw)) : 0;
        const floorRaw = Number(String(floorSource?.dataset?.e3dcFloor || globalReserve?.dataset?.e3dcFloor || '').replace(',', '.'));
        if (applyFloor && Number.isFinite(floorRaw) && floorRaw > 0 && floorRaw <= 100 && next < floorRaw) {
            next = Math.ceil(floorRaw);
        }
        return String(Math.max(0, Math.min(100, Math.round(next))));
    }

    function globalReserveValue(commit = true) {
        const value = clampHouseReserveValue(globalReserve?.value, globalReserve, commit);
        if (commit) {
            if (globalReserve && globalReserve.value !== value) globalReserve.value = value;
            const reserveInput = document.querySelector('#vehicleAssignmentForm input[name="wbminsoc"]');
            if (reserveInput && reserveInput.value !== value) reserveInput.value = value;
        }
        return value || '0';
    }

    function globalPriceValue() {
        return fmt(globalPrice?.value) || '0';
    }

    function syncSimpleGlobalSubmitFields() {
        const reserve = globalReserveValue(true);
        const price = globalPriceValue();
        document.querySelectorAll('[data-simple-house-reserve-submit]').forEach(input => {
            input.value = reserve;
        });
        document.querySelectorAll('[data-simple-price-limit-submit]').forEach(input => {
            input.value = price;
        });
    }

    function setBadgeState(element, text, variant = 'success') {
        if (!element) return;
        const classes = {
            success: 'badge bg-success-subtle text-success border border-success-subtle rounded-pill',
            warning: 'badge bg-warning-subtle text-warning border border-warning-subtle rounded-pill',
            danger: 'badge bg-danger-subtle text-danger border border-danger-subtle rounded-pill',
            info: 'badge bg-info-subtle text-info border border-info-subtle rounded-pill'
        };
        element.className = classes[variant] || classes.success;
        element.textContent = text;
    }

    function setModeState(form, text, variant = 'success') {
        setBadgeState(form.querySelector('[data-simple-mode-state]'), text, variant);
    }

    function setGlobalStatus(text, variant = 'success') {
        const status = document.querySelector('[data-simple-global-status]');
        setBadgeState(status, text, variant);
        if (status) status.classList.add('px-3', 'py-2');
    }

    function updatePanels(form) {
        const unit = selectedUnit(form);
        form.querySelectorAll('[data-simple-target-panel]').forEach(panel => {
            panel.hidden = panel.getAttribute('data-simple-target-panel') !== unit;
        });
        updateStatus(form);
    }

    function updateStatus(form) {
        const status = form.querySelector('[data-simple-status-text]');
        if (!status) return;
        const unit = selectedUnit(form);
        const ready = fmt(form.querySelector('[name="simple_ready_by"]')?.value) || '--:--';
        const reserve = globalReserveValue(false);
        const price = globalPriceValue();
        const energy = selectedEnergy(form);
        const intent = selectedIntent(form);
        const planActive = form.dataset.simplePlanActive === '1';
        const targetText = unit === 'kwh'
            ? `Lademenge ${fmt(form.querySelector('[name="simple_target_kwh"]')?.value) || '0'} kWh`
            : `Auto-Ziel ${fmt(form.querySelector('[name="simple_target_soc"]')?.value) || '0'}%`;
        let ruleText = 'Betriebsart: Überschuss · PV nach Ladekurve, Hausakku zuerst';
        if (intent === 'off') {
            if (energy === 'pv_battery') {
                ruleText = `Betriebsart: Beobachten · PV + Akku bis ${reserve}% Hausakku-Reserve, keine Ladebefehle`;
            } else {
                ruleText = 'Betriebsart: Beobachten · keine Ladebefehle, Speicher folgt eigener Regelung';
            }
        } else if (intent === 'instant') {
            if (energy === 'grid_price') {
                ruleText = `Betriebsart: Sofort · Netz bis ${price} ct/kWh`;
            } else if (energy === 'pv_battery') {
                ruleText = `Betriebsart: Sofort · PV + Akku bis ${reserve}% Hausakku-Reserve, Netz bleibt aus`;
            } else {
                ruleText = 'Betriebsart: Sofort · nur PV-Überschuss';
            }
        } else if (intent === 'scheduled') {
            if (energy === 'grid_price') {
                ruleText = planActive
                    ? `Betriebsart: Fertig bis · Netz erlaubt, günstige Zeiten bis ${ready}`
                    : 'Betriebsart: Fertig bis · Netz erlaubt, Ladeplan speichern';
                setModeState(form, planActive ? 'Ladeplan aktiv' : 'Ladeplan speichern', planActive ? 'info' : 'warning');
            } else if (energy === 'pv_battery') {
                ruleText = `Betriebsart: Fertig bis · PV + Akku bis ${ready}, Hausakku-Reserve ${reserve}%, Netz bleibt aus`;
            } else {
                ruleText = 'Betriebsart: Fertig bis · nur PV-Überschuss, keine Netzgarantie';
            }
        } else if (energy === 'pv_battery') {
            ruleText = `Betriebsart: Überschuss · PV + Akku bis ${reserve}% Hausakku-Reserve, Netz bleibt aus`;
        }
        let valuesText = '';
        if (energy === 'pv_battery' && intent === 'off') {
            valuesText = `Unterhalb ${reserve}% stützt der Akku nur Hausverbrauch und Wärmepumpe; ohne Auto gilt wieder die Ladekurve`;
        } else if (energy === 'pv_battery') {
            valuesText = `Unterhalb ${reserve}% stützt der Akku nur Hausverbrauch und Wärmepumpe`;
        } else if (intent === 'scheduled') {
            valuesText = `Planwerte: ${targetText} bis ${ready}`;
        } else if (intent === 'instant' && energy === 'grid_price') {
            valuesText = `Sofortlimit: Netz bis ${price} ct/kWh`;
        }
        const ruleEl = status.querySelector('[data-simple-rule-text]');
        const valuesEl = status.querySelector('[data-simple-values-text]');
        if (ruleEl && valuesEl) {
            ruleEl.textContent = ruleText;
            valuesEl.textContent = valuesText;
            valuesEl.classList.toggle('d-none', valuesText === '');
        } else {
            status.textContent = valuesText ? `${ruleText} · ${valuesText}` : ruleText;
        }
    }

    function updateAllStatus() {
        forms.forEach(updateStatus);
    }

    function markPlanDirty(form, input) {
        if (!form || !input) return;
        const name = input.getAttribute('name') || '';
        if (!name || name === 'simple_energy_mode' || name === 'simple_charge_intent') return;
        if (selectedIntent(form) === 'scheduled') {
            form.dataset.simplePlanActive = '0';
        }
    }

    function rememberSimpleMode(form) {
        form.dataset.simpleSavedEnergy = selectedEnergy(form);
        form.dataset.simpleSavedIntent = selectedIntent(form);
    }

    function restoreSimpleMode(form) {
        const energy = form.dataset.simpleSavedEnergy || 'pv';
        const intent = form.dataset.simpleSavedIntent || 'surplus';
        const energyRadio = form.querySelector(`[name="simple_energy_mode"][value="${energy}"]`);
        const intentRadio = form.querySelector(`[name="simple_charge_intent"][value="${intent}"]`);
        if (energyRadio) energyRadio.checked = true;
        if (intentRadio) intentRadio.checked = true;
        updateStatus(form);
    }

    function simpleNativeModeValue(energy, intent) {
        if (intent === 'off') return '0';
        if (energy === 'grid_price') return '5';
        if (energy === 'pv_battery' && intent === 'scheduled') return '12';
        if (energy === 'pv_battery') return '4';
        return '2';
    }

    function simpleObserveStoragePolicyValue(energy, intent) {
        return (intent === 'off' && energy === 'pv_battery') ? 'reserve' : 'curve';
    }

    function syncAdvancedModeFromSimple(form, energy, intent) {
        const wb = form.querySelector('[name="simple_wb_id"]')?.value || '1';
        const nextMode = simpleNativeModeValue(energy, intent);
        const nextObservePolicy = simpleObserveStoragePolicyValue(energy, intent);
        form.dataset.simpleNativeMode = nextMode;
        const modeSelect = document.getElementById('wb' + wb + 'ModeSelect');
        const observeSelect = document.getElementById('wb' + wb + 'ObserveStoragePolicy');
        const lockSwitch = document.getElementById('wb' + wb + 'LockSwitch');
        if (modeSelect) {
            modeSelect.value = nextMode;
            modeSelect.dataset.savedMode = String(nextMode);
            if (typeof updateWbModeHelp === 'function') updateWbModeHelp(wb);
        }
        if (observeSelect) {
            observeSelect.value = nextObservePolicy;
            observeSelect.dataset.savedPolicy = nextObservePolicy;
        }
        if (lockSwitch) {
            lockSwitch.checked = true;
        }
        if (typeof syncPriceLimitGuard === 'function') {
            syncPriceLimitGuard();
        }
    }

    function simpleStateForAdvancedMode(mode, observePolicy = 'curve') {
        const value = String(mode || '0');
        if (value === '0') return {energy: observePolicy === 'reserve' ? 'pv_battery' : 'pv', intent: 'off'};
        if (value === '12') return {energy: 'pv_battery', intent: 'scheduled'};
        if (value === '4' || value === '9' || value === '10') return {energy: 'pv_battery', intent: 'surplus'};
        if (value === '5' || value === '11') return {energy: 'grid_price', intent: 'instant'};
        return {energy: 'pv', intent: 'surplus'};
    }

    function setSimpleRadio(form, name, value) {
        const radio = form.querySelector(`[name="${name}"][value="${value}"]`);
        if (radio) radio.checked = true;
    }

    function normalizeSimpleChoice(form, changedInput = null) {
        if (!form) return;
        if (selectedEnergy(form) === 'grid_price' && selectedIntent(form) === 'surplus') {
            if (changedInput && changedInput.getAttribute('name') === 'simple_charge_intent') {
                setSimpleRadio(form, 'simple_energy_mode', 'pv');
            } else {
                setSimpleRadio(form, 'simple_charge_intent', 'scheduled');
            }
        }
    }

    function syncSimpleModeFromAdvanced(wb, mode) {
        const form = document.querySelector('.wallbox-simple-card input[name="simple_wb_id"][value="' + wb + '"]')?.closest('.wallbox-simple-card');
        if (!form) return;
        const observeSelect = document.getElementById('wb' + wb + 'ObserveStoragePolicy');
        const state = simpleStateForAdvancedMode(mode, observeSelect?.value || 'curve');
        form.dataset.simpleNativeMode = String(mode || '0');
        setSimpleRadio(form, 'simple_energy_mode', state.energy);
        setSimpleRadio(form, 'simple_charge_intent', state.intent);
        if (String(mode) === '12') {
            const departureInput = document.getElementById('wb' + wb + 'BatteryDepartureTime');
            const readyInput = form.querySelector('[name="simple_ready_by"]');
            if (departureInput && readyInput) readyInput.value = departureInput.value || '06:30';
        }
        form.dataset.simplePlanActive = '0';
        rememberSimpleMode(form);
        updatePanels(form);
        setModeState(form, 'Betriebsart gespeichert', 'success');
    }

    function syncSimpleDepartureFromAdvanced(wb) {
        const modeSelect = document.getElementById('wb' + wb + 'ModeSelect');
        if (!modeSelect || String(modeSelect.value) !== '12') return;
        const departureInput = document.getElementById('wb' + wb + 'BatteryDepartureTime');
        const form = document.querySelector('.wallbox-simple-card input[name="simple_wb_id"][value="' + wb + '"]')?.closest('.wallbox-simple-card');
        const readyInput = form ? form.querySelector('[name="simple_ready_by"]') : null;
        if (departureInput && readyInput) {
            readyInput.value = departureInput.value || '06:30';
            updateStatus(form);
        }
    }

    function syncSimpleGlobalFromAdvanced() {
        const reserveInput = document.querySelector('#vehicleAssignmentForm input[name="wbminsoc"]');
        if (reserveInput && globalReserve) {
            globalReserve.value = reserveInput.value || globalReserve.value || '70';
            updateAllStatus();
        }
    }

    function syncSimpleVehicleFromAdvanced(wb) {
        const form = document.querySelector('.wallbox-simple-card input[name="simple_wb_id"][value="' + wb + '"]')?.closest('.wallbox-simple-card');
        if (!form) return;
        const copyValue = (sourceSelector, targetSelector) => {
            const source = document.querySelector(sourceSelector);
            const target = form.querySelector(targetSelector);
            if (source && target) target.value = source.value;
        };
        const carSelect = document.querySelector(`.car-selector[data-wb="${wb}"]`);
        const simpleCarSelect = form.querySelector('[name="simple_vehicle_id"]');
        if (carSelect && simpleCarSelect) {
            simpleCarSelect.value = carSelect.value || '__none';
            const selectedOption = carSelect.options[carSelect.selectedIndex];
            const nameInput = form.querySelector('[data-simple-vehicle-name]');
            if (nameInput && selectedOption) nameInput.value = selectedOption.dataset.name || selectedOption.text || carSelect.value;
        }
        copyValue(`[name="wb${wb}_capacity"]`, '[data-simple-capacity]');
        copyValue(`[name="wb${wb}_charge_power"]`, '[data-simple-charge-power]');
        copyValue(`[name="wb${wb}_target_soc"]`, '[data-simple-target-soc]');
        updateStatus(form);
    }

    function syncSimpleAssignmentFromAdvanced(wb = null) {
        syncSimpleGlobalFromAdvanced();
        const targets = wb ? [String(wb)] : forms.map(form => form.querySelector('[name="simple_wb_id"]')?.value).filter(Boolean);
        targets.forEach(syncSimpleVehicleFromAdvanced);
    }

    function syncAdvancedGlobalsFromSimple() {
        const reserveInput = document.querySelector('#vehicleAssignmentForm input[name="wbminsoc"]');
        if (reserveInput && globalReserve) reserveInput.value = globalReserveValue(true);
        const priceInput = document.getElementById('wallboxPriceLimit');
        if (priceInput && globalPrice) {
            priceInput.value = globalPriceValue();
            if (typeof syncPriceLimitGuard === 'function') syncPriceLimitGuard();
        }
        syncSimpleGlobalSubmitFields();
    }

    function setSimpleHouseReserve(value = null, source = null) {
        const reserveInput = document.querySelector('#vehicleAssignmentForm input[name="wbminsoc"]');
        if (value !== null && value !== undefined) {
            if (globalReserve) globalReserve.value = value;
            if (reserveInput) reserveInput.value = value;
        }
        const next = clampHouseReserveValue(globalReserve?.value ?? reserveInput?.value, source || globalReserve || reserveInput, true);
        if (globalReserve) globalReserve.value = next;
        if (reserveInput) reserveInput.value = next;
        syncSimpleGlobalSubmitFields();
        updateAllStatus();
        return next;
    }

    window.e3dcSimpleWallboxSync = {
        modeFromAdvanced: syncSimpleModeFromAdvanced,
        departureFromAdvanced: syncSimpleDepartureFromAdvanced,
        globalsFromAdvanced: syncSimpleGlobalFromAdvanced,
        assignmentFromAdvanced: syncSimpleAssignmentFromAdvanced,
        globalsFromSimple: syncAdvancedGlobalsFromSimple,
        refreshGlobalSubmitFields: syncSimpleGlobalSubmitFields,
        setHouseReserve: setSimpleHouseReserve,
        reserveValue: globalReserveValue
    };

    window.setWallboxHouseReserve = function(source = 'simple') {
        const reserveInput = document.querySelector('#vehicleAssignmentForm input[name="wbminsoc"]');
        const rawValue = source === 'advanced'
            ? (reserveInput?.value ?? globalReserve?.value ?? '70')
            : (globalReserve?.value ?? reserveInput?.value ?? '70');
        const sourceInput = source === 'advanced' ? reserveInput : globalReserve;
        setSimpleHouseReserve(rawValue, sourceInput);
        saveSimpleGlobalLimits(0);
    };

    function saveSimpleOperatingMode(form) {
        normalizeSimpleChoice(form);
        const energy = selectedEnergy(form);
        const intent = selectedIntent(form);
        if (intent === 'off' && form.dataset.simpleSavedIntent !== 'off') {
            const observeReserve = energy === 'pv_battery'
                ? ' Der Storage Manager führt den Speicher bis zur Hausakku-Reserve; ohne Auto gilt wieder die Ladekurve.'
                : ' Der Speicher folgt seiner normalen Ladekurve.';
            if (!window.confirm('WB' + (form.querySelector('[name="simple_wb_id"]')?.value || '') + ' in Beobachten wechseln? E3DC-Control sendet dann keine Ladebefehle.' + observeReserve)) {
                restoreSimpleMode(form);
                return;
            }
        }
        if (intent === 'instant' && energy === 'grid_price') {
            if (!window.confirm('Sofortladen mit Netz erlaubt starten? Netzstrom wird nur bis zum eingestellten Preislimit genutzt.')) {
                restoreSimpleMode(form);
                return;
            }
        }
        if (intent === 'scheduled') {
            form.dataset.simplePlanActive = '0';
            setModeState(form, 'Ladeplan speichern', 'warning');
            updateStatus(form);
            return;
        }

        const wbIdx = form.querySelector('[name="simple_wb_id"]')?.value || '1';
        const previousMode = String(form.dataset.simpleNativeMode || '');
        const nextMode = String(simpleNativeModeValue(energy, intent));
        syncSimpleGlobalSubmitFields();
        const formData = new FormData();
        formData.append('save_simple_wallbox_mode_ajax', '1');
        formData.append('simple_wb_id', wbIdx);
        formData.append('simple_energy_mode', energy);
        formData.append('simple_charge_intent', intent);
        formData.append('simple_house_reserve', globalReserveValue(true));
        formData.append('simple_price_limit', globalPriceValue());
        setModeState(form, 'Speichert...', 'warning');
        fetch(WALLBOX_AJAX_ENDPOINT, {
            method: 'POST',
            body: formData
        }).then(res => {
            if (!res.ok) throw new Error('Speichern fehlgeschlagen');
            return res.text();
        }).then(text => {
            if (text.trim() === 'PLAN_REQUIRED') {
                form.dataset.simplePlanActive = '0';
                setModeState(form, 'Ladeplan speichern', 'warning');
                return;
            }
            form.dataset.simplePlanActive = '0';
            rememberSimpleMode(form);
            syncAdvancedModeFromSimple(form, energy, intent);
            if (previousMode !== nextMode) setWallboxPauseUi(wbIdx, false, false);
            setModeState(form, 'Betriebsart gespeichert', 'success');
        }).catch(() => {
            setModeState(form, 'Speichern fehlgeschlagen', 'danger');
        });
    }

    function saveSimpleGlobalLimits(delay = 500) {
        if (globalSaveTimer) window.clearTimeout(globalSaveTimer);
        setGlobalStatus('Speichert...', 'warning');
        globalSaveTimer = window.setTimeout(() => {
            syncAdvancedGlobalsFromSimple();
            const formData = new FormData();
            formData.append('save_simple_wallbox_limits_ajax', '1');
            formData.append('simple_house_reserve', globalReserveValue(true));
            formData.append('simple_price_limit', globalPriceValue());
            fetch(WALLBOX_AJAX_ENDPOINT, {
                method: 'POST',
                body: formData
            }).then(res => {
            if (!res.ok) throw new Error('Speichern fehlgeschlagen');
            setGlobalStatus('Gespeichert', 'success');
            if (window.e3dcSimpleWallboxSync) window.e3dcSimpleWallboxSync.globalsFromSimple();
        }).catch(() => {
                setGlobalStatus('Fehler beim Speichern', 'danger');
            });
        }, delay);
    }

    function applySimpleVehicle(select) {
        const form = select.closest('.wallbox-simple-card');
        const option = select.options[select.selectedIndex];
        if (!form || !option) return;
        const setIf = (selector, value, onlyIfEmpty = false) => {
            const input = form.querySelector(selector);
            if (!input || value === undefined || value === null || value === '') return;
            if (onlyIfEmpty && fmt(input.value) !== '') return;
            input.value = value;
        };
        setIf('[data-simple-capacity]', option.dataset.capacity);
        setIf('[data-simple-charge-power]', option.dataset.power);
        setIf('[data-simple-target-soc]', option.dataset.target, true);
        setIf('[data-simple-current-soc]', option.dataset.soc);
        const nameInput = form.querySelector('[data-simple-vehicle-name]');
        if (nameInput) nameInput.value = option.dataset.name || option.text || select.value;
        updateStatus(form);
    }

    forms.forEach(form => {
        rememberSimpleMode(form);
        form.querySelectorAll('[name="simple_target_unit"]').forEach(radio => {
            radio.addEventListener('change', () => updatePanels(form));
        });
        form.querySelectorAll('[name="simple_energy_mode"], [name="simple_charge_intent"]').forEach(radio => {
            radio.addEventListener('change', () => {
                normalizeSimpleChoice(form, radio);
                updateStatus(form);
                saveSimpleOperatingMode(form);
            });
        });
        form.querySelectorAll('input, select').forEach(input => {
            input.addEventListener('input', () => {
                markPlanDirty(form, input);
                updateStatus(form);
            });
            input.addEventListener('change', () => {
                markPlanDirty(form, input);
                updateStatus(form);
            });
        });
        form.querySelectorAll('[data-simple-car-selector]').forEach(select => {
            select.addEventListener('change', () => applySimpleVehicle(select));
            applySimpleVehicle(select);
        });
        normalizeSimpleChoice(form);
        updatePanels(form);
    });

    if (globalReserve) {
        globalReserve.addEventListener('input', () => {
            updateAllStatus();
            setGlobalStatus('Nicht gespeichert', 'warning');
        });
        globalReserve.addEventListener('change', () => {
            globalReserve.value = globalReserveValue(true);
            updateAllStatus();
            saveSimpleGlobalLimits(0);
        });
    }
    if (globalPrice) {
        globalPrice.addEventListener('input', () => {
            updateAllStatus();
            saveSimpleGlobalLimits(650);
        });
        globalPrice.addEventListener('change', () => {
            updateAllStatus();
            saveSimpleGlobalLimits(0);
        });
    }
}

function confirmSimpleWallboxSubmit(form) {
    if (window.e3dcSimpleWallboxSync && typeof window.e3dcSimpleWallboxSync.refreshGlobalSubmitFields === 'function') {
        window.e3dcSimpleWallboxSync.refreshGlobalSubmitFields();
    }
    const wb = form ? (form.querySelector('[name="simple_wb_id"]')?.value || '') : '';
    const intent = form ? (form.querySelector('[name="simple_charge_intent"]:checked')?.value || 'surplus') : 'surplus';
    const energy = form ? (form.querySelector('[name="simple_energy_mode"]:checked')?.value || 'pv') : 'pv';
    if (intent === 'off') {
        return window.confirm('WB' + wb + ' in Beobachten wechseln? E3DC-Control sendet dann keine Ladebefehle.');
    }
    if (intent === 'instant' && energy === 'grid_price') {
        return window.confirm('Sofortladen mit Netz erlaubt starten? Netzstrom wird nur bis zum eingestellten Preislimit genutzt.');
    }
    return true;
}

var initialPlanHash = "<?= $currentPlanHash ?>";
const WALLBOX_HIDDEN_POLL_INTERVAL_MS = 10000;
const WALLBOX_LIVE_STATUS_INTERVAL_MS = 2000;
const WALLBOX_LIVE_DATA_INTERVAL_MS = 4000;
let wallboxLiveStatusPollTimer = null;
let wallboxLiveDataPollTimer = null;

function wallboxPollInterval(activeIntervalMs) {
    return document.hidden ? WALLBOX_HIDDEN_POLL_INTERVAL_MS : activeIntervalMs;
}

function scheduleWallboxLiveStatusPoll(runNow = false) {
    if (wallboxLiveStatusPollTimer) clearInterval(wallboxLiveStatusPollTimer);
    if (runNow) updateWallboxLiveStatus();
    wallboxLiveStatusPollTimer = setInterval(updateWallboxLiveStatus, wallboxPollInterval(WALLBOX_LIVE_STATUS_INTERVAL_MS));
}

function scheduleWallboxLiveDataPoll(runNow = false) {
    if (wallboxLiveDataPollTimer) clearInterval(wallboxLiveDataPollTimer);
    if (runNow) updateUIFromLiveData();
    wallboxLiveDataPollTimer = setInterval(updateUIFromLiveData, wallboxPollInterval(WALLBOX_LIVE_DATA_INTERVAL_MS));
}

function scheduleWallboxVisibilityPolling(runNow = false) {
    scheduleWallboxLiveStatusPoll(runNow);
    scheduleWallboxLiveDataPoll(runNow);
}

document.addEventListener('visibilitychange', function() {
    scheduleWallboxVisibilityPolling(!document.hidden);
});

function updateWallboxLiveStatus() {
    fetch('get_live_json.php')
        .then(response => response.json())
        .then(data => {
            // Prüfen ob sich der Ladeplan geändert hat
            if (data.wb_plan_hash && initialPlanHash && data.wb_plan_hash !== initialPlanHash) {
                location.reload();
                return;
            }

            const statusCard = document.getElementById('wb-live-status');
            const valDisplay = document.getElementById('live-wb-val');
            const pulse     = document.getElementById('status-pulse');

            const wbPower = parseFloat(data.wb) || 0;

            if (wbPower > 10) { // Schwelle von 10W um Rauschen zu vermeiden
                if (statusCard) statusCard.style.display = 'block';
                if (pulse) pulse.classList.add('pulse-active');

                // Formatierung der Watt-Zahl
                if (valDisplay) {
                    if (wbPower >= 1000) {
                        valDisplay.innerText = (wbPower / 1000).toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' kW';
                    } else {
                        valDisplay.innerText = Math.round(wbPower) + ' W';
                    }
                }
            } else {
                if (statusCard) statusCard.style.display = 'none';
                if (pulse) pulse.classList.remove('pulse-active');
            }
        })
        .catch(err => console.error('Fehler beim Abruf des Wallbox-Status:', err));
}

// Aktiv schnell aktualisieren, im Hintergrund schonender pollen.
scheduleWallboxLiveStatusPoll(true);

// NEU: Skript für die Auswahl der Ladekosten-Anzeige
(function() {
    const selector = document.getElementById('cost-power-selector');
    if (!selector) return;

    const buttons = selector.querySelectorAll('button');
    const details = document.querySelectorAll('.cost-detail');

    function showCost(powerKey) {
            // Speichere die Auswahl im Local Storage
            try {
                localStorage.setItem('lastSelectedWallboxPower', powerKey);
            } catch (e) {
                console.warn("Could not save to localStorage", e);
            }
        buttons.forEach(btn => {
            if (btn.dataset.powerKey === powerKey) {
                btn.classList.remove('btn-outline-info');
                btn.classList.add('btn-info');
            } else {
                btn.classList.remove('btn-info');
                btn.classList.add('btn-outline-info');
            }
        });

        details.forEach(detail => {
            const detailId = 'cost-detail-' + powerKey.replace('.', '_');
                // Wichtig: 'flex' beibehalten, da es vom vorherigen Fix kommt
                detail.style.display = (detail.id === detailId) ? 'flex' : 'none';
        });
    }

    buttons.forEach(btn => {
        btn.addEventListener('click', function() {
            showCost(this.dataset.powerKey);
        });
    });

    // Tooltips initialisieren
    window.addEventListener('DOMContentLoaded', function() {
        if (typeof bootstrap !== 'undefined') {
            var tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'))
            var tooltipList = tooltipTriggerList.map(function (tooltipTriggerEl) {
                return new bootstrap.Tooltip(tooltipTriggerEl)
            })

            // Lade die letzte Auswahl oder setze den Standard auf 11.0 kW
            let lastPower = '11.0';
            try {
                lastPower = localStorage.getItem('lastSelectedWallboxPower') || '11.0';
            } catch (e) {
                // localStorage might be disabled
            }
            showCost(lastPower);
        }
    });
})();

// Fahrzeugzuordnung direkt unter die Wallbox-Auswahl setzen.
function placeVehicleAssignmentCard() {
    const vehicleCard = document.getElementById('vehicle-assignment-card');
    const planningCard = document.getElementById('load-planning-card');
    if (vehicleCard && planningCard && planningCard.parentNode && vehicleCard.nextElementSibling !== planningCard) {
        planningCard.parentNode.insertBefore(vehicleCard, planningCard);
    }
}
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', placeVehicleAssignmentCard);
} else {
    placeVehicleAssignmentCard();
}

// --- Fahrzeug und Wallbox Management ---

// Globale Variablen für Fahrzeug-Daten
let availableVehicles = [];
const wallboxSavedVehicleOptions = <?= json_encode($vehicleSelectBrowserOptions, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) ?>;
const wb1CarSaved = <?= json_encode(canonicalWallboxVehicleSelection($wallboxConfig['wb1_car_id'] ?: '__none', $saved_cars)) ?>;
const wb2CarSaved = <?= json_encode(canonicalWallboxVehicleSelection($wallboxConfig['wb2_car_id'] ?: '__none', $saved_cars)) ?>;

function updateUIFromLiveData() {
    fetch('get_live_json.php')
        .then(response => response.json())
        .then(data => {
            if (data.wb_plan_hash && initialPlanHash && data.wb_plan_hash !== initialPlanHash) {
                location.reload();
                return;
            }

            // --- Fahrzeuge in Dropdowns befüllen ---
            if (data.vehicles && Array.isArray(data.vehicles)) {
                availableVehicles = data.vehicles;
                updateCarSelectors(data);
            }
            syncWallboxPauseFromLiveData(data);
        });
}

function wallboxPauseValue(value) {
    return value === true || value === 1 || value === '1' || value === 'true' || value === 'on';
}

function syncWallboxPauseFromLiveData(data) {
    if (!data || typeof setWallboxPauseUi !== 'function') return;
    [
        {wb: '1', key: 'wb_manual_pause'},
        {wb: '2', key: 'wb2_manual_pause'}
    ].forEach(item => {
        if (!(item.key in data)) return;
        const pending = document.querySelector('[data-wallbox-pause-button][data-wb-id="' + item.wb + '"]:disabled');
        if (pending) return;
        setWallboxPauseUi(item.wb, wallboxPauseValue(data[item.key]), false);
    });
}

function liveAssignedCarId(data, wbIdx) {
    const key = `wb${wbIdx}_assigned_car_id`;
    const value = data && data[key] !== undefined && data[key] !== null ? String(data[key]).trim() : '';
    return value && value !== 'null' ? value : ((wbIdx == 1) ? wb1CarSaved : wb2CarSaved);
}

function normalizedVehicleDropdownOption(raw) {
    if (!raw || !raw.id) return null;
    const id = String(raw.id);
    const name = String(raw.name || raw.label || raw.cloud_vehicle_name || id).trim();
    return {
        id,
        profile_id: raw.profile_id,
        name: name || id,
        label: raw.label,
        soc: raw.soc,
        capacity: raw.capacity ?? raw.capacity_kwh,
        power: raw.power ?? raw.charge_power ?? raw.charge_power_kw,
        max_phases: raw.max_phases,
        target_soc: raw.target_soc ?? raw.targetSoc,
        max_soc: raw.max_soc ?? raw.max_soc_si ?? raw.target_soc
    };
}

function mergedVehicleDropdownOptions() {
    const byId = new Map();
    [...wallboxSavedVehicleOptions, ...availableVehicles].forEach(raw => {
        const vehicle = normalizedVehicleDropdownOption(raw);
        if (!vehicle) return;
        const previous = byId.get(vehicle.id) || {};
        const previousName = String(previous.name || '').trim();
        const vehicleName = String(vehicle.name || '').trim();
        const genericCustomName = /^custom_\d+$/.test(vehicleName);
        byId.set(vehicle.id, {
            ...previous,
            ...vehicle,
            name: (!genericCustomName && vehicleName) ? vehicleName : (previousName || vehicleName || vehicle.id),
            label: vehicle.label || previous.label
        });
    });
    return [...byId.values()];
}

function vehicleProfileForSelection(carId, option = null) {
    const selectedCar = mergedVehicleDropdownOptions().find(v => String(v.id) === String(carId) || String(v.profile_id || '') === String(carId));
    return {
        capacity: selectedCar?.capacity ?? selectedCar?.capacity_kwh ?? option?.dataset.capacity,
        power: selectedCar?.power ?? selectedCar?.charge_power ?? selectedCar?.charge_power_kw ?? option?.dataset.power,
        targetSoc: selectedCar?.target_soc ?? selectedCar?.targetSoc ?? option?.dataset.target,
        maxSoc: selectedCar?.max_soc ?? selectedCar?.max_soc_si ?? selectedCar?.target_soc ?? option?.dataset.max
    };
}

function applyVehicleProfileToInputs(wbIdx, carId, option = null) {
    if (!carId || carId === '__none') return;
    const profile = vehicleProfileForSelection(carId, option);
    const setIfPresent = (name, value) => {
        const input = document.querySelector(`input[name="wb${wbIdx}_${name}"]`);
        if (input && value !== undefined && value !== null && value !== '') input.value = value;
    };
    setIfPresent('capacity', profile.capacity);
    setIfPresent('charge_power', profile.power);
    setIfPresent('target_soc', profile.targetSoc);
    setIfPresent('max_soc_si', profile.maxSoc);
}

function updateCarSelectors(liveData = {}) {
    const selectors = document.querySelectorAll('.car-selector');
    selectors.forEach(sel => {
        const wbIdx = sel.dataset.wb;
        const assignedFromServer = liveAssignedCarId(liveData, wbIdx);
        const currentSaved = assignedFromServer || ((wbIdx == 1) ? wb1CarSaved : wb2CarSaved);
        const otherSelector = document.querySelector(`.car-selector[data-wb="${wbIdx == 1 ? 2 : 1}"]`);
        const otherValue = otherSelector ? otherSelector.value : null;
        const recentUserChange = Number(sel.dataset.userSelectedAt || 0) > 0 && (Date.now() - Number(sel.dataset.userSelectedAt)) < 15000;

        // Bisherige Auswahl merken
        const previousValue = sel.value || '__none';
        const desiredValue = recentUserChange ? previousValue : (currentSaved || previousValue || '__none');

        // Optionen neu aufbauen
        while (sel.options.length > 2) sel.remove(2);

        const seen = new Set(['__none', 'none']);
        mergedVehicleDropdownOptions().forEach(v => {
            if (!v || !v.id) return;
            const vehicleId = String(v.id);
            if (seen.has(vehicleId)) return;
            seen.add(vehicleId);
            // Nur hinzufügen, wenn nicht in der anderen Wallbox gewählt
            if (vehicleId === desiredValue || vehicleId !== otherValue || otherValue === '__none' || otherValue === 'none') {
                let opt = document.createElement('option');
                opt.value = vehicleId;
                opt.dataset.name = v.name || vehicleId;
                opt.dataset.capacity = v.capacity ?? v.capacity_kwh ?? '';
                opt.dataset.power = v.power ?? v.charge_power ?? v.charge_power_kw ?? '';
                opt.dataset.maxPhases = v.max_phases ?? '';
                opt.dataset.target = v.target_soc ?? v.targetSoc ?? '';
                opt.dataset.max = v.max_soc ?? v.max_soc_si ?? v.target_soc ?? '';
                opt.text = v.label || ((v.name || vehicleId) + (v.soc !== null && v.soc !== undefined && !isNaN(v.soc) ? ` (${Math.round(v.soc)}%)` : ''));
                sel.appendChild(opt);
            }
        });

        if (![...sel.options].some(opt => opt.value === desiredValue) && desiredValue && desiredValue !== '__none' && desiredValue !== 'none') {
            let opt = document.createElement('option');
            opt.value = desiredValue;
            opt.dataset.name = desiredValue;
            opt.text = 'Gespeichert: ' + desiredValue;
            sel.appendChild(opt);
        }

        if ([...sel.options].some(opt => opt.value === desiredValue)) {
            sel.value = desiredValue;
        } else {
            sel.value = '__none';
        }
        if (sel.value !== previousValue) {
            applyVehicleProfileToInputs(wbIdx, sel.value, sel.options[sel.selectedIndex]);
        }
    });
}

function setVehicleAssignmentStatus(text, variant = 'info') {
    const status = document.getElementById('vehicleAssignmentStatus');
    if (!status) return;
    const classes = {
        info: 'badge bg-info-subtle text-info border border-info-subtle',
        success: 'badge bg-success-subtle text-success border border-success-subtle',
        warning: 'badge bg-warning-subtle text-warning border border-warning-subtle',
        danger: 'badge bg-danger-subtle text-danger border border-danger-subtle'
    };
    status.className = classes[variant] || classes.info;
    status.textContent = text;
}

function appendVehicleFormValue(formData, name) {
    const el = document.querySelector(`[name="${name}"]`);
    if (el) formData.append(name, el.value);
}

function saveVehicleAssignment(wbIdx = null, options = {}) {
    const formData = new FormData();
    formData.append('save_soc_settings', '1');
    formData.append('response_format', 'json');
    appendVehicleFormValue(formData, 'wbminsoc');

    const targets = wbIdx ? [Number(wbIdx)] : [1, 2];
    targets.forEach(idx => {
        appendVehicleFormValue(formData, `wb${idx}_car_id`);
        appendVehicleFormValue(formData, `wb${idx}_capacity`);
        appendVehicleFormValue(formData, `wb${idx}_charge_power`);
        appendVehicleFormValue(formData, `wb${idx}_target_soc`);
        appendVehicleFormValue(formData, `wb${idx}_max_soc_si`);
        appendVehicleFormValue(formData, `manual_soc_wb${idx}`);
    });

    if (!options.silent) setVehicleAssignmentStatus('Speichert...', 'warning');
    return fetch(WALLBOX_AJAX_ENDPOINT, {
        method: 'POST',
        body: formData
    }).then(async res => {
        let payload = null;
        try { payload = await res.json(); } catch (_) { payload = null; }
        if (!res.ok || !payload || payload.ok !== true) {
            const code = payload && payload.code ? String(payload.code) : 'http_' + res.status;
            const error = new Error('Speichern fehlgeschlagen (' + code + ')');
            error.code = code;
            throw error;
        }
        if (!options.silent) setVehicleAssignmentStatus('Gespeichert', 'success');
        if (window.e3dcSimpleWallboxSync) window.e3dcSimpleWallboxSync.assignmentFromAdvanced(wbIdx);
        return payload;
    }).catch(err => {
        if (!options.silent) setVehicleAssignmentStatus('Fehler: ' + String(err.code || 'unbekannt'), 'danger');
        throw err;
    });
}

// Ereignisbehandlung für gegenseitigen Ausschluss, automatisches Ausfüllen und Speichern
document.querySelectorAll('.car-selector').forEach(sel => {
    sel.addEventListener('change', function() {
        this.dataset.userSelectedAt = String(Date.now());
        const wbIdx = this.dataset.wb;
        const otherWbIdx = wbIdx == 1 ? 2 : 1;
        const otherSel = document.querySelector(`.car-selector[data-wb="${otherWbIdx}"]`);

        if (otherSel && otherSel.value === this.value && this.value !== 'none' && this.value !== '__none') {
            otherSel.value = '';
            otherSel.value = '__none';
            updateCarSelectors();
            saveVehicleAssignment(otherWbIdx, {silent: true}).catch(() => {});
        }

        // Auto-Fill für gespeicherte Vorlagen und Fahrzeuge mit Profilwerten
        if (this.value && this.value !== '__none') {
            const selectedOpt = this.options[this.selectedIndex];
            applyVehicleProfileToInputs(wbIdx, this.value, selectedOpt);
        }
        saveVehicleAssignment(wbIdx).catch(() => {});
    });
});

document.querySelectorAll('#vehicleAssignmentForm input[name^="wb1_"], #vehicleAssignmentForm input[name^="wb2_"]').forEach(el => {
    el.addEventListener('change', function() {
        const match = this.name.match(/^wb([12])_/);
        if (match) saveVehicleAssignment(Number(match[1])).catch(() => {});
    });
});

const wbMinSocInput = document.querySelector('#vehicleAssignmentForm input[name="wbminsoc"]');
if (wbMinSocInput) {
    wbMinSocInput.addEventListener('change', function() {
        if (window.e3dcSimpleWallboxSync && typeof window.e3dcSimpleWallboxSync.setHouseReserve === 'function') {
            this.value = window.e3dcSimpleWallboxSync.setHouseReserve(this.value, this);
        }
        saveVehicleAssignment(null).catch(() => {});
    });
}

function setManualSoC(wbIdx) {
    const input = document.getElementById('manual_soc_wb' + wbIdx);
    const soc = input ? input.value : '';
    if (!soc || soc === '') return;

    const selector = document.querySelector(`.car-selector[data-wb="${wbIdx}"]`);
    const carId = selector ? selector.value : '';

    let carName = carId;
    if (selector && selector.selectedIndex >= 0) {
        const selectedOpt = selector.options[selector.selectedIndex];
        if (selectedOpt.dataset.name) {
            carName = selectedOpt.dataset.name;
        } else if (carId === 'none') {
            carName = 'Gast-Fahrzeug';
        } else if (carId === '') {
            carName = 'Manuell';
        }
    }

    const capacityInput = document.querySelector(`input[name="wb${wbIdx}_capacity"]`);
    const capacity = capacityInput ? capacityInput.value : '0';

    const formData = new FormData();
    formData.append('save_manual_soc', '1');
    formData.append('wb_index', wbIdx);
    formData.append('manual_soc_value', soc);
    formData.append('manual_car_id', carId);
    formData.append('manual_car_name', carName);
    formData.append('manual_car_capacity', capacity);

    saveVehicleAssignment(wbIdx, {silent: true}).catch(() => {}).then(() => fetch(WALLBOX_AJAX_ENDPOINT, {
        method: 'POST',
        body: formData
    })).then(res => res.text()).then(() => {
        alert(`✓ Manueller SoC für Wallbox ${wbIdx} gesetzt.`);
        window.location.reload();
    }).catch(err => alert('Fehler beim Speichern: ' + err));
}

function updateWbModeHelp(wbIdx) {
    const modeSelect = document.getElementById('wb' + wbIdx + 'ModeSelect');
    const helpBox = document.getElementById('wb' + wbIdx + 'ModeHelp');
    if (!modeSelect || !helpBox) return;
    const option = modeSelect.options[modeSelect.selectedIndex];
    helpBox.textContent = option && option.dataset.help ? option.dataset.help : '';
    const departureBox = document.getElementById('wb' + wbIdx + 'BatteryDepartureBox');
    if (departureBox) departureBox.classList.toggle('d-none', String(modeSelect.value) !== '12');
    const observeStorageBox = document.getElementById('wb' + wbIdx + 'ObserveStorageBox');
    if (observeStorageBox) observeStorageBox.classList.toggle('d-none', String(modeSelect.value) !== '0');
}

function setWallboxPauseUi(wbIdx, paused, pending = false) {
    document.querySelectorAll('[data-wallbox-pause-button][data-wb-id="' + wbIdx + '"]').forEach(button => {
        button.dataset.paused = paused ? '1' : '0';
        button.classList.toggle('btn-warning', paused);
        button.classList.toggle('btn-outline-secondary', !paused);
        button.disabled = pending;
        button.title = paused ? 'Automatik fortsetzen' : 'Wallbox manuell pausieren';
        button.setAttribute('aria-label', 'Wallbox ' + wbIdx + (paused ? ' fortsetzen' : ' pausieren'));
        const icon = button.querySelector('i');
        if (icon) {
            icon.className = 'fas ' + (pending ? 'fa-spinner fa-spin' : (paused ? 'fa-play' : 'fa-pause'));
        }
    });
    document.querySelectorAll('.wallbox-simple-card input[name="simple_wb_id"][value="' + wbIdx + '"]').forEach(input => {
        const form = input.closest('.wallbox-simple-card');
        const badge = form ? form.querySelector('[data-wallbox-pause-badge]') : null;
        if (badge) badge.classList.toggle('d-none', !paused);
    });
}

function saveWallboxManualPause(wbIdx, paused) {
    const formData = new FormData();
    formData.append('save_wb_manual_pause_ajax', '1');
    formData.append('wb_id', wbIdx);
    formData.append('manual_pause', paused ? '1' : '0');
    setWallboxPauseUi(wbIdx, paused, true);
    return fetch(WALLBOX_AJAX_ENDPOINT, {
        method: 'POST',
        body: formData
    }).then(res => {
        if (!res.ok) throw new Error('Network error');
        return res.json();
    }).then(data => {
        setWallboxPauseUi(wbIdx, !!data.manual_pause, false);
    }).catch(() => {
        setWallboxPauseUi(wbIdx, !paused, false);
        alert('Fehler beim Speichern der Wallbox-Pause!');
    });
}

document.querySelectorAll('[data-wallbox-pause-button]').forEach(button => {
    button.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        const wbIdx = button.dataset.wbId || '1';
        const paused = button.dataset.paused === '1';
        saveWallboxManualPause(wbIdx, !paused);
    });
});

function toggleWbMode(wbIdx) {
    const lockSwitch = document.getElementById('wb' + wbIdx + 'LockSwitch');
    const modeSelect = document.getElementById('wb' + wbIdx + 'ModeSelect');
    const observeSelect = document.getElementById('wb' + wbIdx + 'ObserveStoragePolicy');
    updateWbModeHelp(wbIdx);

    // Checkbox checked = AN = not locked. Checkbox unchecked = AUS = locked!
    const isLocked = lockSwitch.checked ? '0' : '1';
    const mode = modeSelect.value;
    const previousMode = String(modeSelect.dataset.savedMode || '');
    const observePolicy = observeSelect ? (observeSelect.value || 'curve') : 'curve';

    // UI-Rückmeldung aktualisieren und Bedienung während des Speicherns sperren
    lockSwitch.disabled = true;
    modeSelect.disabled = true;
    if (observeSelect) observeSelect.disabled = true;

    const formData = new FormData();
    formData.append('save_wb_status_ajax', '1');
    formData.append('wb_id', wbIdx);
    formData.append('wb_locked', isLocked);
    formData.append('wb_mode', mode);
    formData.append('wb_observe_storage_policy', observePolicy);
    const departureInput = document.getElementById('wb' + wbIdx + 'BatteryDepartureTime');
    const departureWindow = document.getElementById('wb' + wbIdx + 'BatteryDepartureWindow');
    if (departureInput && mode === '12') {
        formData.append('wb_battery_departure_time', departureInput.value || '06:30');
    }
    if (departureWindow && mode === '12') {
        formData.append('wb_battery_departure_window_h', departureWindow.value || '3');
    }

    fetch(WALLBOX_AJAX_ENDPOINT, {
        method: 'POST',
        body: formData
    }).then(res => {
        if (!res.ok) throw new Error('Network error');
        return res.text();
    }).then(data => {
        console.log(`WB${wbIdx} Mode/Lock saved.`);
        if (previousMode !== String(mode)) setWallboxPauseUi(wbIdx, false, false);
        modeSelect.dataset.savedMode = String(mode);
        if (observeSelect) observeSelect.dataset.savedPolicy = observePolicy;
        if (window.e3dcSimpleWallboxSync) window.e3dcSimpleWallboxSync.modeFromAdvanced(wbIdx, mode);
    }).catch(err => {
        alert('Fehler beim Speichern der Wallbox-Einstellungen!');
    }).finally(() => {
        lockSwitch.disabled = false;
        modeSelect.disabled = false;
        if (observeSelect) observeSelect.disabled = false;
    });
}

function saveWbBatteryDeparture(wbIdx) {
    const departureInput = document.getElementById('wb' + wbIdx + 'BatteryDepartureTime');
    const departureWindow = document.getElementById('wb' + wbIdx + 'BatteryDepartureWindow');
    const formData = new FormData();
    formData.append('save_wb_status_ajax', '1');
    formData.append('wb_id', wbIdx);
    formData.append('wb_battery_departure_time', departureInput ? (departureInput.value || '06:30') : '06:30');
    formData.append('wb_battery_departure_window_h', departureWindow ? (departureWindow.value || '3') : '3');

    fetch(WALLBOX_AJAX_ENDPOINT, {
        method: 'POST',
        body: formData
    }).then(res => {
        if (!res.ok) throw new Error('Network error');
        return res.text();
    }).then(() => {
        if (window.e3dcSimpleWallboxSync) window.e3dcSimpleWallboxSync.departureFromAdvanced(wbIdx);
    }).catch(() => {
        alert('Fehler beim Speichern der Abfahrtszeit!');
    });
}

function setWbPriorityStatus(text, variant = 'secondary') {
    const status = document.getElementById('wbPriorityStatus');
    if (!status) return;
    const classes = {
        secondary: 'badge bg-secondary-subtle text-secondary border border-secondary-subtle',
        warning: 'badge bg-warning-subtle text-warning border border-warning-subtle',
        success: 'badge bg-success-subtle text-success border border-success-subtle',
        danger: 'badge bg-danger-subtle text-danger border border-danger-subtle'
    };
    status.className = classes[variant] || classes.secondary;
    status.textContent = text;
}

function saveWbPriority(mode) {
    const radios = Array.from(document.querySelectorAll('input[name="wallboxPriority"]'));
    if (!radios.length || radios.some(radio => radio.disabled)) return;
    const status = document.getElementById('wbPriorityStatus');
    const savedMode = status ? String(status.dataset.savedMode || '0') : '0';
    const formData = new FormData();
    formData.append('save_wb_priority_ajax', '1');
    formData.append('wb_native_mode', mode);
    radios.forEach(radio => radio.disabled = true);
    setWbPriorityStatus('Speichert...', 'warning');

    fetch(WALLBOX_AJAX_ENDPOINT, {
        method: 'POST',
        body: formData
    }).then(res => {
        if (!res.ok) throw new Error('Network error');
        return res.text();
    }).then(() => {
        if (status) status.dataset.savedMode = String(mode);
        radios.forEach(radio => { radio.defaultChecked = String(radio.value) === String(mode); });
        setWbPriorityStatus('Gespeichert', 'success');
    }).catch(() => {
        radios.forEach(radio => { radio.checked = String(radio.value) === savedMode; });
        setWbPriorityStatus('Fehler', 'danger');
        alert('Fehler beim Speichern der Ladepriorität!');
    }).finally(() => {
        radios.forEach(radio => radio.disabled = false);
    });
}

function saveWbName(wbIdx, newName) {
    const formData = new FormData();
    formData.append('save_wb_status_ajax', '1');
    formData.append('wb_id', wbIdx);
    formData.append('wb_name', newName);

    fetch(WALLBOX_AJAX_ENDPOINT, {
        method: 'POST',
        body: formData
    }).catch(err => alert('Fehler beim Speichern des Namens!'));
}

function syncPriceLimitGuard() {
    const input = document.getElementById('wallboxPriceLimit');
    const guard = document.getElementById('wallboxPriceLimitGuard');
    if (!input || !guard) return;
    const raw = String(input.value || '').replace(',', '.');
    const value = Number.parseFloat(raw);
    const modeSelects = [document.getElementById('wb1ModeSelect'), document.getElementById('wb2ModeSelect')].filter(Boolean);
    const priceModeSelected = modeSelects.some(select => String(select.value) === '5');
    let text = 'Sofortladen nutzt Netz nur bis zum Preislimit; geplante Ladefenster ignorieren dieses Limit.';
    let cls = 'form-text small mt-1 text-muted';

    if (!Number.isFinite(value) || value <= 0) {
        text = priceModeSelected
            ? 'Sofort bis Preislimit startet kein Netzladen: Preislimit ist 0 ct/kWh. Geplante Ladefenster laden trotzdem.'
            : '0 ct blockiert spontanes Netzladen; geplante Ladefenster laden trotzdem.';
        cls = 'form-text small mt-1 text-warning fw-semibold';
    } else if (value < 5) {
        text = 'Sehr niedriges Preislimit: spontanes Netzladen wird fast immer warten; geplante Ladefenster bleiben aktiv.';
        cls = 'form-text small mt-1 text-warning fw-semibold';
    } else if (value > 80) {
        text = 'Sehr hohes Preislimit: Sofort bis Preislimit erlaubt Netzladen fast immer; geplante Ladefenster bleiben unverändert.';
        cls = 'form-text small mt-1 text-warning fw-semibold';
    } else if (priceModeSelected) {
        text = `Sofort bis Preislimit nutzt Netz nur bis ${value.toFixed(1)} ct/kWh; geplante Ladefenster laden unabhängig davon.`;
        cls = 'form-text small mt-1 text-info';
    }

    guard.textContent = text;
    guard.className = cls;
}

scheduleWallboxLiveDataPoll(false);
document.addEventListener('DOMContentLoaded', function() {
    initWallboxViewToggle();
    initSimpleWallboxTargetControls();
    updateUIFromLiveData();
    updateWbModeHelp(1);
    updateWbModeHelp(2);
    syncPriceLimitGuard();
    const priceLimitInput = document.getElementById('wallboxPriceLimit');
    if (priceLimitInput) priceLimitInput.addEventListener('input', syncPriceLimitGuard);
    [document.getElementById('wb1ModeSelect'), document.getElementById('wb2ModeSelect')].filter(Boolean).forEach(select => {
        select.addEventListener('change', syncPriceLimitGuard);
    });
});
</script>
