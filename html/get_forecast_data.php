<?php
require_once __DIR__ . '/helpers.php';
requireWebAuth(true);
if (session_status() === PHP_SESSION_ACTIVE) {
    @session_write_close();
}
header('Content-Type: application/json');
date_default_timezone_set('Europe/Berlin');

$paths = getInstallPaths();
$basePath = rtrim($paths['install_path'], '/') . '/';

$file = (!empty($_GET['file']) && $_GET['file'] !== 'undefined') ? basename($_GET['file']) : 'awattardebug.txt';
$filepath = $basePath . $file;

$ecoScoreFile = '/var/www/html/ramdisk/eco_score.json';
$v4_prices_available = file_exists($ecoScoreFile) && (time() - filemtime($ecoScoreFile) < 3600);

if (!file_exists($filepath)) {
    // awattardebug.txt ist in V4 deaktiviert (s.u.). Ohne eco_score.json gibt es keine Preise.
    // Trotzdem Forecast-Zeiten liefern, damit Dachflächenprognosen weiterhin sichtbar bleiben.
    // Preise werden in diesem Fall im Diagramm als null angezeigt.
}

$confData = loadE3dcConfig();
$conf = $confData['config'] ?? [];
$hasWb = hasAnyWallboxConfig($conf);

function forecastWallboxConfigFloat($conf, $key, $default) {
    return isset($conf[$key]) ? parseNumericConfigValue($conf[$key], $default) : $default;
}

function forecastWallboxModeIsOff($conf, $wbId) {
    $modeKey = ($wbId === 2) ? 'wb2_mode' : 'wb1_mode';
    $fallback = ($wbId === 1 && isset($conf['wb_native_mode'])) ? $conf['wb_native_mode'] : null;
    $mode = $conf[$modeKey] ?? $fallback;
    return trim((string)$mode) === '0';
}

function forecastWallboxChargePowerW($conf, $wbId) {
    $keys = ($wbId === 2)
        ? ['wb2_charge_power', 'car_charge_power']
        : ['wb1_charge_power', 'car_charge_power'];
    foreach ($keys as $key) {
        if (!isset($conf[$key])) {
            continue;
        }
        $kw = forecastWallboxConfigFloat($conf, $key, 0.0);
        if ($kw > 0.0) {
            return $kw * 1000.0;
        }
    }

    if (isset($conf['wbcostpowers'])) {
        $parts = array_map('trim', explode(',', (string)$conf['wbcostpowers']));
        $idx = max(0, $wbId - 1);
        if (isset($parts[$idx])) {
            $kw = parseNumericConfigValue($parts[$idx], 0.0);
            if ($kw > 0.0) {
                return $kw * 1000.0;
            }
        }
    }

    return 11000.0;
}

function loadNativeWallboxPlannedPower($conf) {
    $files = [
        '/var/www/html/ramdisk/native_wallbox_schedule.json' => null,
        '/var/www/html/ramdisk/native_wallbox_schedule_wb1.json' => 1,
        '/var/www/html/ramdisk/native_wallbox_schedule_wb2.json' => 2,
    ];
    $planned = [];

    foreach ($files as $file => $fallbackWbId) {
        if (!file_exists($file) || !is_readable($file)) {
            continue;
        }
        $decoded = @json_decode(file_get_contents($file), true);
        if (!is_array($decoded)) {
            continue;
        }
        foreach ($decoded as $entry) {
            if (!is_array($entry) || empty($entry['ts'])) {
                continue;
            }
            $ts = (int)floor(((int)$entry['ts']) / 900) * 900;
            $wbId = isset($entry['wb_id']) ? (int)$entry['wb_id'] : (int)$fallbackWbId;
            if ($wbId !== 1 && $wbId !== 2) {
                $wbId = 1;
            }
            if (forecastWallboxModeIsOff($conf, $wbId)) {
                continue;
            }

            $powerW = forecastWallboxChargePowerW($conf, $wbId);
            if ($powerW <= 0.0) {
                continue;
            }
            if (!isset($planned[$ts])) {
                $planned[$ts] = [1 => 0.0, 2 => 0.0];
            }
            $planned[$ts][$wbId] = max($planned[$ts][$wbId] ?? 0.0, $powerW);
        }
    }

    return $planned;
}

$plannedWallboxPowerByTs = loadNativeWallboxPlannedPower($conf);

$awmwst = isset($conf['awmwst']) ? parseNumericConfigValue($conf['awmwst'], 19.0) : 19.0;
$awneben = isset($conf['awnebenkosten']) ? parseNumericConfigValue($conf['awnebenkosten'], 0.0) : 0.0;
$stromtarifTyp = strtolower(trim((string)($conf['stromtarif_typ'] ?? 'static')));
$batCapacity = isset($conf['speichergroesse']) ? parseNumericConfigValue($conf['speichergroesse'], 10.0) : 10.0;
$multiplier = $batCapacity * 40.0; // Umrechnung von "% der Speicherkapazität" in Watt

if (file_exists($filepath)) {
    $sourceTs = filemtime($filepath);
    // V4: awattardebug.txt (Legacy) wird nicht mehr geparst.
    // Python epex_manager.py schreibt eco_score.json mit korrekten UNIX-Timestamps.
    // Dadurch entfaellt die fehleranfaellige GMT-Dezimalstunden-Konvertierung des C++ Kerns.
    $lines = [];
} else {
    $sourceTs = time();
    $lines = [];
}

$merged = [];
$currentBlock = '';
$lastTRaw = ['Simulation' => -1, 'Data' => -1, 'DV' => -1];
$dayOffset = ['Simulation' => 0, 'Data' => 0, 'DV' => 0];

foreach ($lines as $line) {
    $line = trim($line);
    if (empty($line)) continue;
    if (strpos($line, 'Simulation') === 0) { $currentBlock = 'Simulation'; continue; }
    if (strpos($line, 'Data') === 0) { $currentBlock = 'Data'; continue; }
    if (strpos($line, 'DV') === 0) { $currentBlock = 'DV'; continue; }

    if (empty($currentBlock)) continue;

    $parts = preg_split('/\s+/', $line);
    if (count($parts) < 2) continue;
    if (!is_numeric(str_replace(',', '.', $parts[0]))) continue;

    $tRaw = (float)str_replace(',', '.', $parts[0]);

    // Tageswechsel (Sprung von z.B. 23.75 auf 0.00) erkennen
    if ($tRaw <= 48) {
        if ($lastTRaw[$currentBlock] !== -1 && $tRaw < $lastTRaw[$currentBlock] && ($lastTRaw[$currentBlock] - $tRaw) > 12) {
            $dayOffset[$currentBlock] += 24;
        }
        $lastTRaw[$currentBlock] = $tRaw;
        $absTime = $tRaw + $dayOffset[$currentBlock];
    } else {
        $absTime = $tRaw;
    }

    $key = number_format($absTime, 2, '.', '');

    if (!isset($merged[$key])) {
        $merged[$key] = [
                'tRaw' => $absTime, 'price' => 0, 'soc' => 0, 'bat' => 0,
                'pv' => 0, 'home' => 0, 'wp' => 0, 'climate' => 0, 'temp' => 0, 'wb' => 0, 'wb2' => 0,
                'dv_grid_kwh' => null
        ];
    }

    if ($currentBlock === 'Simulation' && count($parts) >= 5) {
        $merged[$key]['soc'] = (float)str_replace(',', '.', $parts[2]);
        $merged[$key]['bat'] = (float)str_replace(',', '.', $parts[3]) * $multiplier;
    } elseif ($currentBlock === 'Data' && count($parts) >= 6) {
        $priceRaw = (float)str_replace(',', '.', $parts[1]);
        $merged[$key]['price'] = calculateAwattarPrice($priceRaw, $sourceTs, $awmwst, $awneben);
        $merged[$key]['home'] = (float)str_replace(',', '.', $parts[2]) * $multiplier;
        $merged[$key]['wp'] = (float)str_replace(',', '.', $parts[3]) * $multiplier;
        $merged[$key]['pv'] = (float)str_replace(',', '.', $parts[4]) * $multiplier;
        $merged[$key]['temp'] = (float)str_replace(',', '.', $parts[5]);
        if ($hasWb && count($parts) >= 7) {
            $merged[$key]['wb'] = (float)str_replace(',', '.', $parts[6]) * $multiplier;
        }
    } elseif ($currentBlock === 'DV' && count($parts) >= 6) {
        $merged[$key]['dv_grid_kwh'] = (float)str_replace(',', '.', $parts[5]);
    }
}

// V4 Zukunfts-Puffer: Immer 72h ab jetzt vorausschauen.
// $now auf volle Stunde runden = saubere Slot-Grenzen für den Preis-Join mit eco_score.json.
$now = floor(time() / 3600) * 3600;
$limit = $now + (72 * 3600); // Immer 72h ab jetzt (robust, unabhaengig von Mitternacht)

$ecoScoresTemp = $v4_prices_available ? @json_decode(file_get_contents($ecoScoreFile), true) : [];
$offsetHours = (int)date('Z') / 3600;

for ($ts = $now; $ts < $limit; $ts += 900) {
    // V4: echte Unix-Zeit als Key verwenden.
    // Der alte GMT-Stunden-Key wiederholte sich jeden Tag und liess dadurch
    // im 72h-Fenster Slots gleicher Uhrzeit kollidieren. Dann fehlten nach
    // 48h Verbrauch, Batterie und SoC oder wurden mit 0 dargestellt.
    $key = (string)$ts;

    // Falls awattardebug.txt diesen Zeitpunkt nicht enthaelt, fuellen wir ihn mit V4-Leerstruktur auf.
    if (!isset($merged[$key])) {
        // WICHTIG: tRaw als Unix-Timestamp speichern (> 10000), NICHT als Dezimalstunde!
        // So umgeht der Filter-Code die stale $midnightGmt-Berechnung und nutzt direkt $ts.
        // Ohne diesen Fix werden Übermorgen-Slots fälschlich als "Vergangenheit" gelöscht.
        $merged[$key] = [
            'tRaw' => (float)$ts, 'price' => 0, 'soc' => null, 'bat' => null,
            'pv' => null, 'home' => null, 'wp' => null, 'climate' => null, 'temp' => null, 'wb' => 0, 'wb2' => 0, 'dv_grid_kwh' => null
        ];

        // Preis direkt befüllen
        if (!empty($ecoScoresTemp) && is_array($ecoScoresTemp)) {
            foreach ($ecoScoresTemp as $score) {
                if ($ts * 1000 >= $score['start_timestamp'] && $ts * 1000 < $score['end_timestamp']) {
                    $merged[$key]['price'] = $score['billing_price'];
                    break;
                }
            }
        }
    }
}

uksort($merged, function($a, $b) { return (float)$a <=> (float)$b; });

// Vergangene Eintraege entfernen: Prognose-Chart startet immer bei 'jetzt'.
// Live-Vergangenheits-Daten kommen separat aus live_history.txt (solar.js).
$nowForFilter = time();
$offsetHoursForFilter = (int)date('Z') / 3600;
foreach ($merged as $key => $row) {
    $tRaw = $row['tRaw'];
    if ($tRaw > 10000) {
        // Unix-Timestamp
        $slotTs = (int)$tRaw;
    } else {
        // Dezimal-Stunden (GMT) -> Unix
        $midnightGmt = strtotime(gmdate("Y-m-d", $sourceTs) . " 00:00:00 UTC");
        $slotTs = (int)($midnightGmt + ($tRaw * 3600));
    }
    // Slots die mehr als 15 Minuten in der Vergangenheit liegen -> raus
    if ($slotTs < ($nowForFilter - 900)) {
        unset($merged[$key]);
    }
}

$totalLines = count($merged);
$step = max(1, floor($totalLines / 400));
$count = 0;
$last_dv_kwh = null;

$data = [
    'labels' => [], 'timestamps' => [], 'pv' => [], 'home' => [], 'bat' => [], 'grid' => [], 'soc' => [],
    'storage_target_curve' => [],
    'market_charge' => [], 'market_hold' => [], 'market_action' => [],
    'wp' => [], 'climate' => [], 'wb' => [], 'wb2' => [], 'price' => [], 'market_price' => [], 'eco_score' => [], 'dv_grid' => [],
    'home_source' => [], 'home_quality' => [],
    'wp_source' => [], 'wp_quality' => [],
    'climate_source' => [], 'climate_quality' => [],
    'direct_marketing_export' => [], 'direct_marketing_charge' => [], 'direct_marketing_soc' => [], 'direct_marketing_action' => [],
    'direct_marketing_candidate' => [], 'direct_marketing_candidate_w' => [], 'direct_marketing_candidate_action' => [],
    'direct_marketing_selected' => [], 'direct_marketing_executable' => [], 'direct_marketing_commands_allowed' => [],
    'direct_marketing_plan_executable' => [], 'direct_marketing_plan_commands_allowed' => [],
    'direct_marketing_block_reason' => [], 'direct_marketing_planned_w' => [],
    'direct_marketing_authorized_export_w' => [], 'direct_marketing_active' => [],
    'direct_marketing_hardware_effect' => [], 'direct_marketing_window_id' => [],
    'direct_marketing_selection_invariant' => [],
    'direct_marketing_export_segment_id' => [],
    'direct_marketing_market_eligible' => [], 'direct_marketing_market_window_id' => [],
    'direct_marketing_market_window_start_ts' => [], 'direct_marketing_market_window_end_ts' => [],
    'direct_marketing_market_margin_class' => [], 'direct_marketing_market_net_sell_ct' => [],
    'predump' => [], 'predump_w' => [],
    'predump_candidate_w' => [], 'predump_executable_w' => [], 'predump_status' => [],
    'storage_slot_id' => [],
    'pv_e3dc_dc' => [], 'pv_external_ac' => [],
    'pv_e3dc_dc_p50' => [], 'pv_external_ac_p50' => [],
    'pv_topology_status' => [], 'pv_topology_reason' => [], 'pv_topology_revision' => [],
    'pv_topology_source' => [], 'pv_topology_quality' => [],
    'pv_resource_projection_status' => [], 'pv_resource_projection_reason' => [],
    'headroom_dc_pressure_wh' => [], 'headroom_pcc_pressure_wh' => [],
    'headroom_combined_pressure_wh' => [], 'headroom_deadline_ts' => [],
    'pv_m1' => [], 'pv_m2' => [], 'pv_m3' => [], 'pv_ensemble' => [],
    'pv_history' => []   // KI-Prognose Vergangenheits-Overlay (was predicted, now past)

];

function forecastCanonicalDispatchPlanValid($plan) {
    if (!is_array($plan)) return false;
    if (($plan['schema_version'] ?? '') !== 'storage_dispatch_plan_v1') return false;
    $planId = (string)($plan['plan_id'] ?? '');
    return preg_match('/^sha256:[0-9a-f]{64}$/', $planId) === 1
        && isset($plan['slots'])
        && is_array($plan['slots']);
}

function forecastTrajectoryCanonicalize($value) {
    if (!is_array($value)) return $value;
    $keys = array_keys($value);
    $isList = count($value) === 0 || $keys === range(0, count($value) - 1);
    if ($isList) return array_map('forecastTrajectoryCanonicalize', $value);
    ksort($value, SORT_STRING);
    foreach ($value as $key => $item) $value[$key] = forecastTrajectoryCanonicalize($item);
    return $value;
}

function forecastTrajectoryCanonicalJson($value) {
    return json_encode(
        forecastTrajectoryCanonicalize($value),
        JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_PRESERVE_ZERO_FRACTION
    );
}

function forecastPlanCanonicalizePreservingObjects($value) {
    if (is_object($value)) {
        $items = get_object_vars($value);
        ksort($items, SORT_STRING);
        $result = new stdClass();
        foreach ($items as $key => $item) {
            $result->{$key} = forecastPlanCanonicalizePreservingObjects($item);
        }
        return $result;
    }
    if (is_array($value)) {
        return array_map('forecastPlanCanonicalizePreservingObjects', $value);
    }
    return $value;
}

function forecastPlanCanonicalJsonPreservingObjects($value) {
    return json_encode(
        forecastPlanCanonicalizePreservingObjects($value),
        JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_PRESERVE_ZERO_FRACTION
    );
}

function forecastReadStoragePlanActionProjectionArtifact($path, $maxBytes = 524288) {
    if (!is_string($path) || $path === '' || !is_file($path) || !is_readable($path)) return null;
    $size = @filesize($path);
    if (!is_int($size) || $size < 2 || $size > (int)$maxBytes) return null;
    $raw = @file_get_contents($path);
    if (!is_string($raw) || strlen($raw) !== $size) return null;
    $decoded = @json_decode($raw, true);
    $object = @json_decode($raw);
    if (!is_array($decoded) || !is_object($object)) return null;
    return ['data' => $decoded, 'object' => $object, 'raw_json' => $raw];
}

function forecastDirectMarketingActionBindingValid($plan, $projection, $action, $plannedW, $slotStart, $slotEnd, $validFrom, $horizonEnd) {
    $bindings = [
        'ECONOMIC_EXPORT' => ['source_action' => 'eco_plus_export_candidate', 'modes' => ['eco_plus']],
        'PV_STORE' => ['source_action' => 'eco_plus_store_pv_candidate', 'modes' => ['eco', 'eco_plus']],
        'CHARGE_BLOCK_WAIT' => ['source_action' => 'direct_marketing_charge_block_wait', 'modes' => ['eco_plus']],
    ];
    $binding = $bindings[$action] ?? null;
    $direct = is_array($plan['direct_marketing'] ?? null) ? $plan['direct_marketing'] : [];
    $flags = is_array($direct['flags'] ?? null) ? $direct['flags'] : [];
    $decisionHorizon = is_array($plan['shadow_dispatch']['decision_horizon'] ?? null)
        ? $plan['shadow_dispatch']['decision_horizon'] : [];
    $sourceAction = trim((string)($projection['direct_marketing_plan_source_action'] ?? ''));
    $sourceMode = strtolower(str_replace(['-', ' '], '_', trim((string)($projection['direct_marketing_plan_source_mode'] ?? ''))));
    if ($sourceMode === 'eco+' || $sourceMode === 'ecoplus') $sourceMode = 'eco_plus';
    $planMode = strtolower(str_replace(['-', ' '], '_', trim((string)($direct['mode'] ?? ''))));
    if ($planMode === 'eco+' || $planMode === 'ecoplus') $planMode = 'eco_plus';
    $actionId = (string)($projection['direct_marketing_plan_action_id'] ?? '');
    $actionLineageId = (string)($projection['direct_marketing_plan_action_lineage_id'] ?? '');
    $windowId = trim((string)($projection['direct_marketing_window_id'] ?? ''));
    $windowStart = (int)($projection['direct_marketing_window_start_ts_ms'] ?? 0);
    $windowEnd = (int)($projection['direct_marketing_window_end_ts_ms'] ?? 0);
    $segmentId = trim((string)($projection['direct_marketing_plan_segment_id'] ?? ''));
    $horizon = is_array($projection['direct_marketing_action_horizon_contract'] ?? null)
        ? $projection['direct_marketing_action_horizon_contract'] : [];
    $roles = is_array($projection['direct_marketing_action_roles'] ?? null)
        ? $projection['direct_marketing_action_roles'] : [];
    if (!is_array($binding)
        || ($direct['active'] ?? null) !== true
        || ($direct['shadow'] ?? null) !== false
        || ($flags['commands_allowed'] ?? null) !== true
        || $sourceAction !== $binding['source_action']
        || !in_array($sourceMode, $binding['modes'], true)
        || $planMode !== $sourceMode
        || ($projection['direct_marketing_plan_source_action_execution_released'] ?? null) !== true
        || preg_match('/^sha256:[0-9a-f]{64}$/', $actionId) !== 1
        || !hash_equals($actionId, $actionLineageId)
        || $windowId === '' || $segmentId === ''
        || $windowStart <= 0 || $windowEnd <= $windowStart
        || !is_numeric($projection['direct_marketing_requested_w'] ?? null)
        || abs((float)$projection['direct_marketing_requested_w'] - (float)$plannedW) > 0.01
        || ($projection['direct_marketing_candidate'] ?? null) !== true
        || ($projection['direct_marketing_candidate_action'] ?? '') !== $action
        || ($projection['direct_marketing_candidate_only'] ?? null) !== false
        || ($projection['direct_marketing_plan_selected_action'] ?? '') !== $action
        || ($projection['direct_marketing_plan_executable_action'] ?? '') !== $action
        || ($projection['direct_marketing_effective_action'] ?? null) !== null
        || ($projection['direct_marketing_block_reason'] ?? null) !== null) {
        return false;
    }
    if (($horizon['schema_version'] ?? '') !== 'storage_dispatch_action_horizon_v1'
        || ($horizon['action'] ?? '') !== $action
        || ($horizon['complete'] ?? null) !== true
        || ($horizon['block_reason_code'] ?? null) !== null
        || ($horizon['window_source'] ?? '') !== 'canonical_direct_marketing_plan_projection'
        || (int)($horizon['slot_start_ts_ms'] ?? 0) !== $slotStart
        || (int)($horizon['slot_end_ts_ms'] ?? 0) !== $slotEnd
        || (int)($horizon['window_start_ts_ms'] ?? 0) !== $windowStart
        || (int)($horizon['window_end_ts_ms'] ?? 0) !== $windowEnd
        || (int)($decisionHorizon['start_ts_ms'] ?? 0) !== $validFrom
        || (int)($decisionHorizon['end_ts_ms'] ?? 0) < $windowEnd
        || (int)($decisionHorizon['end_ts_ms'] ?? 0) > $horizonEnd
        || (int)($horizon['bound_horizon_start_ts_ms'] ?? 0) !== (int)($decisionHorizon['start_ts_ms'] ?? 0)
        || (int)($horizon['bound_horizon_end_ts_ms'] ?? 0) !== (int)($decisionHorizon['end_ts_ms'] ?? 0)) {
        return false;
    }
    if (($roles['schema_version'] ?? '') !== 'direct_marketing_action_roles_v1'
        || ($roles['status'] ?? '') !== 'CONSISTENT'
        || ($roles['candidate_action'] ?? '') !== $action
        || ($roles['candidate_only'] ?? null) !== false
        || ($roles['plan_selected_action'] ?? '') !== $action
        || ($roles['plan_executable_action'] ?? '') !== $action
        || ($roles['effective_action'] ?? null) !== null
        || ($roles['runtime_effect_claim_allowed'] ?? null) !== false
        || (int)($roles['slot_start_ts_ms'] ?? 0) !== $slotStart
        || (int)($roles['slot_end_ts_ms'] ?? 0) !== $slotEnd) {
        return false;
    }
    $matches = [];
    foreach (($direct['windows'] ?? []) as $window) {
        if (!is_array($window)
            || (string)($window['action'] ?? '') !== $sourceAction
            || (int)($window['start_ts'] ?? 0) !== $windowStart
            || (int)($window['end_ts'] ?? 0) !== $windowEnd) {
            continue;
        }
        $sourceWindowId = trim((string)(
            $action === 'ECONOMIC_EXPORT'
                ? ($window['export_plateau_id'] ?? $window['window_id'] ?? '')
                : ($window['window_id'] ?? '')
        ));
        $projectedSourceWindowId = trim((string)(
            $action === 'ECONOMIC_EXPORT'
                ? ($projection['direct_marketing_export_plateau_id'] ?? '')
                : $windowId
        ));
        if ($sourceWindowId === '' || $sourceWindowId !== $projectedSourceWindowId) continue;
        if (forecastTrajectoryCanonicalJson($window['export_segment_id'] ?? null)
            !== forecastTrajectoryCanonicalJson($projection['direct_marketing_export_segment_id'] ?? null)) {
            continue;
        }
        if ($action === 'PV_STORE'
            && ($window['pv_store_source_contract'] ?? null)
                !== ($projection['direct_marketing_plan_pv_store_source_contract'] ?? null)) {
            continue;
        }
        $matches[] = $window;
    }
    if (count($matches) !== 1) return false;
    $sourceWindow = $matches[0];
    $maxPower = $sourceWindow['max_power_w'] ?? null;
    if ($action !== 'CHARGE_BLOCK_WAIT'
        && (!is_numeric($maxPower) || (float)$maxPower + 0.01 < (float)$plannedW)) {
        return false;
    }
    $sourceSegment = $sourceWindow['export_segment_id'] ?? null;
    if (!$sourceSegment) $sourceSegment = $sourceWindow['segment_id'] ?? null;
    $expectedSegmentId = $sourceSegment ? (string)$sourceSegment : $actionId;
    if ($segmentId !== $expectedSegmentId) return false;

    $identityMaterial = [
        'action' => $action,
        'window_id' => $windowId,
        'window_start_ts_ms' => $windowStart,
        'window_end_ts_ms' => $windowEnd,
    ];
    $exportGate = is_array($projection['direct_marketing_economic_export_gate'] ?? null)
        ? $projection['direct_marketing_economic_export_gate'] : null;
    if ($action !== 'ECONOMIC_EXPORT') {
        if ($exportGate !== null
            || ($projection['direct_marketing_gate_lineage_id'] ?? null) !== null
            || ($projection['direct_marketing_gate_generation'] ?? null) !== null
            || ($projection['direct_marketing_gate_generation_id'] ?? null) !== null) {
            return false;
        }
        $pvContract = $projection['direct_marketing_plan_pv_store_source_contract'] ?? null;
        if ($action === 'PV_STORE' && !in_array($pvContract, ['E3DC_DC', 'E3DC_DC_PLUS_AUX_AC_PV'], true)) {
            return false;
        }
        if ($action !== 'PV_STORE' && $pvContract !== null) return false;
    } else {
        if (!is_array($exportGate)
            || ($exportGate['allowed'] ?? null) !== true
            || !is_array($exportGate['blockers'] ?? null)
            || count($exportGate['blockers']) !== 0
            || ($exportGate['block_reason_code'] ?? null) !== null
            || ($exportGate['policy_commands_allowed'] ?? null) !== true
            || ($exportGate['accounting_contract'] ?? '') !== 'DIRECT_MARKETING_POLICY_ECONOMICS_REUSED_NO_DOUBLE_DEDUCTION'
            || !is_numeric($exportGate['policy_export_budget_w'] ?? null)
            || (float)$exportGate['policy_export_budget_w'] + 0.01 < (float)$plannedW) {
            return false;
        }
        foreach (['margin_ct_kwh', 'user_min_margin_ct', 'expected_profit_eur', 'min_window_profit_eur'] as $key) {
            if (!isset($exportGate[$key]) || !is_numeric($exportGate[$key])
                || !is_finite((float)$exportGate[$key])) return false;
        }
        if ((float)$exportGate['margin_ct_kwh'] + 0.000001 < (float)$exportGate['user_min_margin_ct']
            || (float)$exportGate['expected_profit_eur'] + 0.000001 < (float)$exportGate['min_window_profit_eur']) {
            return false;
        }
        $startGate = is_array($exportGate['export_window_start_gate'] ?? null)
            ? $exportGate['export_window_start_gate'] : [];
        $business = is_array($startGate['business_binding'] ?? null)
            ? $startGate['business_binding'] : [];
        $businessRevision = (string)($business['business_contract_sha256'] ?? '');
        $businessMaterial = $business;
        unset($businessMaterial['business_contract_sha256']);
        $businessEncoded = forecastTrajectoryCanonicalJson($businessMaterial);
        $expectedBusinessRevision = is_string($businessEncoded)
            ? 'sha256:' . hash('sha256', $businessEncoded) : '';
        if (($startGate['schema'] ?? '') !== 'export_window_start_gate_v1'
            || ($startGate['passed'] ?? null) !== true
            || !in_array((string)($startGate['profile'] ?? ''), ['standard', 'aggressive', 'expert'], true)
            || ($startGate['action'] ?? '') !== $sourceAction
            || ($startGate['window_id'] ?? '') !== $windowId
            || ($startGate['business_window_id'] ?? '') !== $windowId
            || (int)($startGate['origin_start_ts'] ?? 0) !== $windowStart
            || (int)($startGate['end_ts'] ?? 0) !== $windowEnd
            || ($startGate['accounting_contract'] ?? '') !== 'START_ONLY_NO_REMAINING_WINDOW_REAPPLICATION'
            || ($business['schema'] ?? '') !== 'direct_marketing_export_business_binding_v1'
            || ($business['action'] ?? '') !== $sourceAction
            || (int)($business['origin_start_ts'] ?? 0) !== $windowStart
            || (int)($business['end_ts'] ?? 0) !== $windowEnd
            || preg_match('/^sha256:[0-9a-f]{64}$/', $businessRevision) !== 1
            || !hash_equals($businessRevision, $expectedBusinessRevision)
            || ($startGate['business_contract_sha256'] ?? null) !== $businessRevision
            || $windowId !== 'export-business:' . substr($businessRevision, 7, 24)) {
            return false;
        }
        $lineage = is_array($exportGate['export_window_gate_lineage'] ?? null)
            ? $exportGate['export_window_gate_lineage'] : [];
        $gateEncoded = forecastTrajectoryCanonicalJson($startGate);
        $gateSha = is_string($gateEncoded) ? 'sha256:' . hash('sha256', $gateEncoded) : '';
        $lineageMaterial = [
            'schema' => 'export_window_gate_lineage_v1',
            'gate_sha256' => $gateSha,
            'action' => $sourceAction,
            'window_id' => $windowId,
            'origin_start_ts' => $windowStart,
            'end_ts' => $windowEnd,
        ];
        $lineageEncoded = forecastTrajectoryCanonicalJson($lineageMaterial);
        $expectedLineageId = is_string($lineageEncoded)
            ? 'sha256:' . hash('sha256', $lineageEncoded) : '';
        $generation = $lineage['current_generation'] ?? null;
        if (!is_int($generation) || $generation < 1) return false;
        $generationEncoded = forecastTrajectoryCanonicalJson([
            'gate_lineage_id' => $expectedLineageId,
            'generation' => $generation,
        ]);
        $expectedGenerationId = is_string($generationEncoded)
            ? 'sha256:' . hash('sha256', $generationEncoded) : '';
        $expectedPreviousId = null;
        if ($generation > 1) {
            $previousEncoded = forecastTrajectoryCanonicalJson([
                'gate_lineage_id' => $expectedLineageId,
                'generation' => $generation - 1,
            ]);
            $expectedPreviousId = is_string($previousEncoded)
                ? 'sha256:' . hash('sha256', $previousEncoded) : '';
        }
        $reasons = $lineage['transition_reason_codes'] ?? null;
        $sortedReasons = is_array($reasons) ? array_values(array_unique($reasons)) : [];
        sort($sortedReasons, SORT_STRING);
        if (($lineage['schema'] ?? '') !== 'export_window_gate_lineage_v1'
            || ($lineage['status'] ?? '') !== 'ACTIVE'
            || ($lineage['effect_contract'] ?? '') !== 'STATUS_ONLY_NO_EXECUTION_AUTHORITY'
            || ($lineage['gate_sha256'] ?? '') !== $gateSha
            || ($lineage['gate_lineage_id'] ?? '') !== $expectedLineageId
            || ($lineage['current_generation_id'] ?? '') !== $expectedGenerationId
            || ($lineage['previous_generation_id'] ?? null) !== $expectedPreviousId
            || ($lineage['action'] ?? '') !== $sourceAction
            || ($lineage['window_id'] ?? '') !== $windowId
            || (int)($lineage['origin_start_ts'] ?? 0) !== $windowStart
            || (int)($lineage['end_ts'] ?? 0) !== $windowEnd
            || !is_array($reasons) || count($reasons) === 0 || $reasons !== $sortedReasons
            || ($projection['direct_marketing_gate_lineage_id'] ?? null) !== $expectedLineageId
            || ($projection['direct_marketing_gate_generation'] ?? null) !== $generation
            || ($projection['direct_marketing_gate_generation_id'] ?? null) !== $expectedGenerationId
            || forecastTrajectoryCanonicalJson($exportGate['action_horizon_contract'] ?? null)
                !== forecastTrajectoryCanonicalJson($horizon)) {
            return false;
        }
        $identityMaterial['gate_lineage_id'] = $expectedLineageId;
        $identityMaterial['gate_generation'] = $generation;
        $identityMaterial['gate_generation_id'] = $expectedGenerationId;
    }
    $identityEncoded = forecastTrajectoryCanonicalJson($identityMaterial);
    $expectedActionId = is_string($identityEncoded)
        ? 'sha256:' . hash('sha256', $identityEncoded) : '';
    return $expectedActionId !== '' && hash_equals($expectedActionId, $actionId);
}

function forecastTrajectoryValidationReason($plan, $source, $planCoherent) {
    if (!$planCoherent || !forecastCanonicalDispatchPlanValid($plan)) return 'DIRECT_MARKETING_CANONICAL_PLAN_INVALID';
    $planId = (string)($plan['plan_id'] ?? '');
    if (!is_array($source)) return 'DIRECT_MARKETING_TRAJECTORY_MISSING';
    if (($source['schema_version'] ?? '') !== 'direct_marketing_trajectory_v1') return 'DIRECT_MARKETING_TRAJECTORY_SCHEMA_INVALID';
    if (($source['active'] ?? null) !== true || ($source['complete'] ?? null) !== true) {
        return $source['reason_code'] ?? $source['status'] ?? 'DIRECT_MARKETING_TRAJECTORY_INCOMPLETE';
    }
    if (($source['plan_id'] ?? null) !== $planId) return 'DIRECT_MARKETING_TRAJECTORY_PLAN_MISMATCH';
    if (!is_array($source['meta'] ?? null) || !is_array($source['slots'] ?? null) || count($source['slots']) === 0) {
        return 'DIRECT_MARKETING_TRAJECTORY_STRUCTURE_INCOMPLETE';
    }
    if (forecastTrajectoryCanonicalJson($source['input_revisions'] ?? null)
        !== forecastTrajectoryCanonicalJson($plan['input_revisions'] ?? null)) {
        return 'DIRECT_MARKETING_TRAJECTORY_INPUT_REVISION_MISMATCH';
    }
    $revision = (string)($source['trajectory_revision'] ?? '');
    $material = $source;
    unset($material['trajectory_revision']);
    $encoded = forecastTrajectoryCanonicalJson($material);
    $calculated = is_string($encoded) ? 'sha256:' . hash('sha256', $encoded) : '';
    if (preg_match('/^sha256:[0-9a-f]{64}$/', $revision) !== 1
        || $calculated === '' || !hash_equals($revision, $calculated)) {
        return 'DIRECT_MARKETING_TRAJECTORY_REVISION_MISMATCH';
    }
    $durationMs = (int)round(((float)($source['slot_duration_s'] ?? 0)) * 1000.0);
    $validFromMs = (int)($source['valid_from_ts_ms'] ?? 0);
    $horizonEndMs = (int)($source['horizon_end_ts_ms'] ?? 0);
    if ($durationMs <= 0 || $validFromMs <= 0 || $horizonEndMs <= $validFromMs) {
        return 'DIRECT_MARKETING_TRAJECTORY_HORIZON_INVALID';
    }
    $planSlots = array_values(array_filter($plan['slots'], function($slot) use ($validFromMs) {
        return is_array($slot) && (int)($slot['start_ts_ms'] ?? 0) >= $validFromMs;
    }));
    if (count($planSlots) !== count($source['slots'])) return 'DIRECT_MARKETING_TRAJECTORY_SLOT_COUNT_MISMATCH';
    $capacityWh = (float)($source['meta']['capacity_wh'] ?? 0);
    $chargeEfficiency = (float)($source['meta']['efficiencies']['charge'] ?? 0);
    $dischargeEfficiency = (float)($source['meta']['efficiencies']['discharge'] ?? 0);
    if (!is_finite($capacityWh) || $capacityWh <= 0.0
        || !is_finite($chargeEfficiency) || $chargeEfficiency <= 0.0
        || !is_finite($dischargeEfficiency) || $dischargeEfficiency <= 0.0) {
        return 'DIRECT_MARKETING_TRAJECTORY_PHYSICS_META_INVALID';
    }
    $previousEnd = null;
    $previousSocEnd = null;
    foreach ($source['slots'] as $index => $slot) {
        $planSlot = $planSlots[$index] ?? null;
        if (!is_array($slot) || !is_array($planSlot)) return 'DIRECT_MARKETING_TRAJECTORY_SLOT_INVALID';
        $start = (int)($slot['start_ts_ms'] ?? 0);
        $end = (int)($slot['end_ts_ms'] ?? 0);
        if (($slot['slot_id'] ?? null) !== ($planSlot['slot_id'] ?? null)
            || $start !== (int)($planSlot['start_ts_ms'] ?? 0)
            || $end !== (int)($planSlot['end_ts_ms'] ?? 0)
            || $end - $start !== $durationMs
            || ($previousEnd !== null && $start !== $previousEnd)) {
            return 'DIRECT_MARKETING_TRAJECTORY_SLOT_BINDING_MISMATCH';
        }
        foreach (['soc_start_pct', 'soc_end_pct', 'battery_w', 'grid_w', 'residual_before_storage_w', 'residual_after_storage_w'] as $key) {
            if (!isset($slot[$key]) || !is_numeric($slot[$key]) || !is_finite((float)$slot[$key])) {
                return 'DIRECT_MARKETING_TRAJECTORY_SLOT_VALUE_INVALID';
            }
        }
        if ((float)$slot['soc_start_pct'] < 0.0 || (float)$slot['soc_start_pct'] > 100.0
            || (float)$slot['soc_end_pct'] < 0.0 || (float)$slot['soc_end_pct'] > 100.0) {
            return 'DIRECT_MARKETING_TRAJECTORY_SOC_OUT_OF_RANGE';
        }
        if ($previousSocEnd !== null && abs((float)$slot['soc_start_pct'] - $previousSocEnd) > 0.0015) {
            return 'DIRECT_MARKETING_TRAJECTORY_SOC_CONTINUITY_INVALID';
        }
        $pv = is_array($slot['pv_w'] ?? null) ? $slot['pv_w'] : [];
        $loads = is_array($slot['loads_w'] ?? null) ? $slot['loads_w'] : [];
        foreach (['total'] as $key) if (!isset($pv[$key]) || !is_numeric($pv[$key])) return 'DIRECT_MARKETING_TRAJECTORY_BALANCE_INPUT_INVALID';
        foreach (['house', 'heat', 'wallbox', 'total'] as $key) if (!isset($loads[$key]) || !is_numeric($loads[$key])) return 'DIRECT_MARKETING_TRAJECTORY_BALANCE_INPUT_INVALID';
        $loadsTotal = (float)$loads['house'] + (float)$loads['heat'] + (float)$loads['wallbox'];
        $expectedGrid = (float)$loads['total'] + (float)$slot['battery_w'] - (float)$pv['total'];
        $expectedResidualBefore = (float)$pv['total'] - (float)$loads['total'];
        $expectedResidualAfter = $expectedResidualBefore - (float)$slot['battery_w'];
        if (abs($loadsTotal - (float)$loads['total']) > 0.01
            || abs($expectedGrid - (float)$slot['grid_w']) > 0.01
            || abs($expectedResidualBefore - (float)$slot['residual_before_storage_w']) > 0.01
            || abs($expectedResidualAfter - (float)$slot['residual_after_storage_w']) > 0.01
            || abs($expectedResidualAfter + (float)$slot['grid_w']) > 0.01) {
            return 'DIRECT_MARKETING_TRAJECTORY_BALANCE_INVALID';
        }
        $slotHours = ($end - $start) / 3600000.0;
        $batteryW = (float)$slot['battery_w'];
        $socDelta = $batteryW >= 0.0
            ? $batteryW * $slotHours * $chargeEfficiency / $capacityWh * 100.0
            : $batteryW * $slotHours / $dischargeEfficiency / $capacityWh * 100.0;
        if (abs(((float)$slot['soc_start_pct'] + $socDelta) - (float)$slot['soc_end_pct']) > 0.01) {
            return 'DIRECT_MARKETING_TRAJECTORY_SOC_PHYSICS_INVALID';
        }
        $selection = is_array($slot['selection'] ?? null) ? $slot['selection'] : [];
        $action = strtoupper((string)($slot['action'] ?? ''));
        $projection = is_array($planSlot['projection'] ?? null) ? $planSlot['projection'] : [];
        $selectedAction = in_array($action, ['PV_STORE', 'ECONOMIC_EXPORT', 'CHARGE_BLOCK_WAIT', 'DV_CURVE_CHARGE'], true);
        $delegation = is_array($slot['delegation'] ?? null) ? $slot['delegation'] : null;
        $delegatedPvStore = $action === 'PV_STORE'
            && is_array($delegation)
            && ($selection['selected'] ?? null) === false
            && ($delegation['schema_version'] ?? '') === 'direct_marketing_future_pv_store_delegation_v1'
            && ($delegation['active'] ?? null) === true
            && ($delegation['commands_allowed'] ?? null) === true
            && ($delegation['action'] ?? null) === 'PV_STORE'
            && ($delegation['pv_store_source_contract'] ?? null) === 'E3DC_DC'
            && ($delegation['no_grid_charge'] ?? null) === true
            && (int)($delegation['valid_until_ts_ms'] ?? 0) >= $end
            && is_numeric($delegation['max_curve_charge_w'] ?? null)
            && (float)$delegation['max_curve_charge_w'] > 0.0;
        if (($selection['selected'] ?? null) === true && $delegation !== null) {
            return 'DIRECT_MARKETING_TRAJECTORY_ACTION_ROLE_AMBIGUOUS';
        }
        if ($selectedAction && !$delegatedPvStore) {
            if (($selection['selected'] ?? null) !== true
                || ($selection['executable'] ?? null) !== true
                || ($selection['commands_allowed'] ?? null) !== true
                || preg_match('/^sha256:[0-9a-f]{64}$/', (string)($selection['action_id'] ?? '')) !== 1) {
                return 'DIRECT_MARKETING_TRAJECTORY_ACTION_NOT_EXECUTABLE';
            }
            $bindingPairs = [
                'action_id' => 'direct_marketing_plan_action_id',
                'window_id' => 'direct_marketing_window_id',
                'segment_id' => 'direct_marketing_plan_segment_id',
                'source_action' => 'direct_marketing_plan_source_action',
                'source_mode' => 'direct_marketing_plan_source_mode',
                'pv_store_source_contract' => 'direct_marketing_plan_pv_store_source_contract',
            ];
            if (($projection['direct_marketing_selected'] ?? null) !== true
                || ($projection['direct_marketing_plan_executable'] ?? null) !== true
                || ($projection['direct_marketing_plan_commands_allowed'] ?? null) !== true
                || strtoupper((string)($projection['direct_marketing_plan_action'] ?? '')) !== $action) {
                return 'DIRECT_MARKETING_TRAJECTORY_PLAN_ACTION_MISMATCH';
            }
            foreach ($bindingPairs as $selectionKey => $projectionKey) {
                if (($selection[$selectionKey] ?? null) !== ($projection[$projectionKey] ?? null)) {
                    return 'DIRECT_MARKETING_TRAJECTORY_ACTION_IDENTITY_MISMATCH';
                }
            }
        } elseif ($action !== 'PASSIVE_NORMAL' && !$delegatedPvStore) {
            return 'DIRECT_MARKETING_TRAJECTORY_ACTION_INVALID';
        } elseif ($action === 'PASSIVE_NORMAL') {
            $passiveBinding = is_array($slot['passive_binding'] ?? null) ? $slot['passive_binding'] : null;
            if (($selection['selected'] ?? null) !== false || $delegation !== null
                || !is_array($passiveBinding)
                || ($passiveBinding['schema'] ?? null) !== 'direct_marketing_passive_normal_binding_v1'
                || forecastTrajectoryCanonicalJson($passiveBinding)
                    !== forecastTrajectoryCanonicalJson($projection['direct_marketing_passive_normal_binding_v1'] ?? null)) {
                return 'DIRECT_MARKETING_TRAJECTORY_PASSIVE_ROLE_INVALID';
            }
        }
        if ($action === 'PV_STORE' || $action === 'DV_CURVE_CHARGE') {
            $dcOnly = $delegatedPvStore || (($selection['pv_store_source_contract'] ?? null) === 'E3DC_DC');
            if ((float)$slot['battery_w'] < -0.01
                || (float)$slot['battery_w'] > (float)$slot['residual_before_storage_w'] + 0.01
                || ($dcOnly && (!isset($pv['e3dc_dc']) || !is_numeric($pv['e3dc_dc'])
                    || (float)$slot['battery_w'] > (float)$pv['e3dc_dc'] + 0.01))) {
                return 'DIRECT_MARKETING_TRAJECTORY_PV_STORE_PHYSICS_INVALID';
            }
            $pvStoreCap = $delegatedPvStore
                ? (float)$delegation['max_curve_charge_w']
                : (float)($selection['requested_w'] ?? 0.0);
            if ($pvStoreCap <= 0.0 || (float)$slot['battery_w'] > $pvStoreCap + 0.01) {
                return 'DIRECT_MARKETING_TRAJECTORY_PV_STORE_CAP_INVALID';
            }
        }
        if ($action === 'ECONOMIC_EXPORT') {
            $requestedW = (float)($selection['requested_w'] ?? 0.0);
            if ($requestedW <= 0.0 || (float)$slot['battery_w'] > 0.01
                || abs((float)$slot['battery_w']) > $requestedW + 0.01) {
                return 'DIRECT_MARKETING_TRAJECTORY_EXPORT_PHYSICS_INVALID';
            }
        }
        if ($action === 'CHARGE_BLOCK_WAIT' && (float)$slot['battery_w'] > 0.01) {
            return 'DIRECT_MARKETING_TRAJECTORY_CHARGE_BLOCK_PHYSICS_INVALID';
        }
        $previousEnd = $end;
        $previousSocEnd = (float)$slot['soc_end_pct'];
    }
    if ($previousEnd !== $horizonEndMs) return 'DIRECT_MARKETING_TRAJECTORY_HORIZON_MISMATCH';
    return null;
}

function forecastDirectMarketingTrajectoryForDisplay($plan, $enabled, $planCoherent) {
    if (!$enabled) {
        return [
            'schema_version' => 'direct_marketing_trajectory_v1',
            'active' => false,
            'complete' => false,
            'status' => 'INACTIVE',
            'reason_code' => 'DIRECT_MARKETING_DISABLED',
            'plan_id' => is_array($plan) ? ($plan['plan_id'] ?? null) : null,
            'meta' => null,
            'slots' => [],
        ];
    }
    $planId = is_array($plan) ? (string)($plan['plan_id'] ?? '') : '';
    $source = is_array($plan) && is_array($plan['direct_marketing_trajectory'] ?? null)
        ? $plan['direct_marketing_trajectory']
        : null;
    $reason = forecastTrajectoryValidationReason($plan, $source, $planCoherent);
    if ($reason === null) {
        return $source;
    }
    $sourceStatus = is_array($source) ? (string)($source['status'] ?? '') : '';
    $projectIncompleteStatus = in_array(
        $sourceStatus,
        ['TRAJECTORY_AXIS_EVIDENCE_LIMIT', 'PASSIVE_POLICY_BINDING_MISSING'],
        true
    ) && $reason === $sourceStatus;
    return [
        'schema_version' => 'direct_marketing_trajectory_v1',
        'active' => true,
        'complete' => false,
        'status' => $projectIncompleteStatus ? $sourceStatus : 'EVIDENCE_LIMIT',
        'reason_code' => $projectIncompleteStatus ? $sourceStatus : $reason,
        'plan_id' => $planId !== '' ? $planId : null,
        'trajectory_revision' => $source['trajectory_revision'] ?? null,
        'input_revisions' => $source['input_revisions'] ?? null,
        'meta' => is_array($source['meta'] ?? null) ? $source['meta'] : null,
        'slots' => [],
    ];
}

function forecastDirectMarketingSelectedActionFallbackForDisplay($plan, $enabled, $planCoherent, $planRawJson = null, $artifactSnapshot = null) {
    $planId = is_array($plan) ? (string)($plan['plan_id'] ?? '') : '';
    $limited = function($reason) use ($enabled, $planId) {
        return [
            'schema_version' => 'direct_marketing_selected_action_fallback_v1',
            'active' => $enabled === true,
            'complete' => false,
            'status' => $enabled ? 'EVIDENCE_LIMIT' : 'INACTIVE',
            'reason_code' => $enabled ? $reason : 'DIRECT_MARKETING_DISABLED',
            'plan_id' => $planId !== '' ? $planId : null,
            'input_revisions' => null,
            'valid_from_ts_ms' => null,
            'horizon_end_ts_ms' => null,
            'slot_duration_s' => null,
            'projection_revision' => null,
            'slots' => [],
        ];
    };
    if (!$enabled) return $limited('DIRECT_MARKETING_DISABLED');
    if (!$planCoherent || !forecastCanonicalDispatchPlanValid($plan)) return $limited('DIRECT_MARKETING_CANONICAL_PLAN_INVALID');
    if (!is_string($planRawJson) || $planRawJson === '' || !is_array($artifactSnapshot)) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_ARTIFACT_MISSING');
    $artifact = is_array($artifactSnapshot['data'] ?? null) ? $artifactSnapshot['data'] : null;
    $artifactObject = is_object($artifactSnapshot['object'] ?? null) ? $artifactSnapshot['object'] : null;
    if (!is_array($artifact) || !is_object($artifactObject)) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_ARTIFACT_INVALID');
    $rootKeys = array_keys($artifact);
    sort($rootKeys, SORT_STRING);
    if ($rootKeys !== ['artifact_revision', 'candidate_effect_allowed', 'consumer_scope', 'control_effect', 'hardware_effect_claim_allowed', 'plan_binding', 'projection', 'projection_revision', 'reason_code', 'runtime_effect_claim_allowed', 'schema_version', 'status']) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_ARTIFACT_INVALID');
    $artifactRevision = (string)($artifact['artifact_revision'] ?? '');
    $artifactMaterial = clone $artifactObject;
    unset($artifactMaterial->artifact_revision);
    $artifactEncoded = forecastPlanCanonicalJsonPreservingObjects($artifactMaterial);
    $artifactCalculated = is_string($artifactEncoded) ? 'sha256:' . hash('sha256', $artifactEncoded) : '';
    if (($artifact['schema_version'] ?? '') !== 'storage_plan_action_projection_v1'
        || ($artifact['consumer_scope'] ?? '') !== 'web_projection'
        || ($artifact['control_effect'] ?? null) !== false
        || ($artifact['runtime_effect_claim_allowed'] ?? null) !== false
        || ($artifact['hardware_effect_claim_allowed'] ?? null) !== false
        || ($artifact['candidate_effect_allowed'] ?? null) !== false
        || preg_match('/^sha256:[0-9a-f]{64}$/', $artifactRevision) !== 1
        || !hash_equals($artifactRevision, $artifactCalculated)) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_ARTIFACT_INVALID');
    $binding = is_array($artifact['plan_binding'] ?? null) ? $artifact['plan_binding'] : null;
    $projection = is_array($artifact['projection'] ?? null) ? $artifact['projection'] : null;
    $projectionObject = is_object($artifactObject->projection ?? null) ? $artifactObject->projection : null;
    if (!is_array($binding) || !is_array($projection) || !is_object($projectionObject)) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_ARTIFACT_INVALID');
    $bindingKeys = array_keys($binding);
    sort($bindingKeys, SORT_STRING);
    if ($bindingKeys !== ['action_axis_revision', 'generated_at_ts_ms', 'horizon_end_ts_ms', 'input_revisions_revision', 'plan_id', 'plan_material_revision', 'plan_schema_version', 'projection_revision', 'raw_plan_sha256', 'raw_plan_size', 'slot_axis_revision', 'slot_duration_s', 'trajectory_revision', 'valid_from_ts_ms', 'valid_until_ts_ms']) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_BINDING_INVALID');
    $projectionKeys = array_keys($projection);
    sort($projectionKeys, SORT_STRING);
    if ($projectionKeys !== ['action_axis_revision', 'active', 'candidate_effect_allowed', 'complete', 'consumer_scope', 'control_effect', 'generated_at_ts_ms', 'hardware_effect_claim_allowed', 'horizon_end_ts_ms', 'input_revisions', 'input_revisions_revision', 'plan_id', 'plan_material_revision', 'projection_revision', 'reason_code', 'runtime_effect_claim_allowed', 'schema_version', 'slot_axis_revision', 'slot_duration_s', 'slots', 'status', 'trajectory_revision', 'valid_from_ts_ms', 'valid_until_ts_ms']) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_BINDING_INVALID');
    $projectionRevision = (string)($projection['projection_revision'] ?? '');
    $projectionMaterial = clone $projectionObject;
    unset($projectionMaterial->projection_revision);
    $projectionEncoded = forecastPlanCanonicalJsonPreservingObjects($projectionMaterial);
    $projectionCalculated = is_string($projectionEncoded) ? 'sha256:' . hash('sha256', $projectionEncoded) : '';
    if (preg_match('/^sha256:[0-9a-f]{64}$/', $projectionRevision) !== 1
        || !hash_equals($projectionRevision, $projectionCalculated)
        || ($artifact['projection_revision'] ?? null) !== $projectionRevision
        || ($binding['projection_revision'] ?? null) !== $projectionRevision
        || ($artifact['status'] ?? null) !== ($projection['status'] ?? null)
        || ($artifact['reason_code'] ?? null) !== ($projection['reason_code'] ?? null)) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_REVISION_INVALID');
    $planArtifactRevision = 'sha256:' . hash('sha256', $planRawJson);
    $inputRevisions = is_array($plan['input_revisions'] ?? null) ? $plan['input_revisions'] : null;
    $inputRevisionEncoded = forecastTrajectoryCanonicalJson($inputRevisions);
    $inputRevisionsRevision = is_string($inputRevisionEncoded) ? 'sha256:' . hash('sha256', $inputRevisionEncoded) : '';
    if (($binding['plan_schema_version'] ?? null) !== ($plan['schema_version'] ?? null)
        || ($binding['plan_id'] ?? null) !== $planId
        || ($binding['plan_material_revision'] ?? null) !== $planId
        || ($binding['raw_plan_sha256'] ?? null) !== $planArtifactRevision
        || (int)($binding['raw_plan_size'] ?? -1) !== strlen($planRawJson)
        || (int)($binding['generated_at_ts_ms'] ?? 0) !== (int)($plan['generated_at_ts_ms'] ?? 0)
        || (int)($binding['valid_from_ts_ms'] ?? 0) !== (int)($plan['valid_from_ts_ms'] ?? 0)
        || (int)($binding['valid_until_ts_ms'] ?? 0) !== (int)($plan['valid_until_ts_ms'] ?? 0)
        || (int)($binding['horizon_end_ts_ms'] ?? 0) !== (int)($plan['horizon_end_ts_ms'] ?? 0)
        || (int)($binding['slot_duration_s'] ?? 0) !== (int)($plan['slot_duration_s'] ?? 0)
        || ($binding['input_revisions_revision'] ?? null) !== $inputRevisionsRevision) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_PLAN_BINDING_INVALID');
    $trajectory = is_array($plan['direct_marketing_trajectory'] ?? null) ? $plan['direct_marketing_trajectory'] : null;
    $trajectoryMaterial = is_array($trajectory) ? $trajectory : [];
    $trajectoryRevision = (string)($trajectoryMaterial['trajectory_revision'] ?? '');
    unset($trajectoryMaterial['trajectory_revision']);
    $trajectoryEncoded = forecastTrajectoryCanonicalJson($trajectoryMaterial);
    $trajectoryCalculated = is_string($trajectoryEncoded) ? 'sha256:' . hash('sha256', $trajectoryEncoded) : '';
    $trajectoryStatus = (string)($trajectory['status'] ?? '');
    $trajectoryMeta = is_array($trajectory['meta'] ?? null) ? $trajectory['meta'] : null;
    $passivePolicyBindingMetaValid = $trajectoryStatus !== 'PASSIVE_POLICY_BINDING_MISSING'
        || (is_array($trajectoryMeta)
            && ($trajectoryMeta['candidate_effect'] ?? null) === false
            && ($trajectoryMeta['shadow_effect'] ?? null) === false
            && ($trajectoryMeta['runtime_authorization_separate'] ?? null) === true);
    if (!is_array($trajectory) || ($trajectory['schema_version'] ?? '') !== 'direct_marketing_trajectory_v1' || ($trajectory['active'] ?? null) !== true || ($trajectory['complete'] ?? null) !== false || !in_array($trajectoryStatus, ['TRAJECTORY_AXIS_EVIDENCE_LIMIT', 'PASSIVE_POLICY_BINDING_MISSING'], true) || !$passivePolicyBindingMetaValid || ($trajectory['reason_code'] ?? null) !== null || !is_array($trajectory['slots'] ?? null) || count($trajectory['slots']) !== 0 || ($trajectory['plan_id'] ?? null) !== $planId || preg_match('/^sha256:[0-9a-f]{64}$/', $trajectoryRevision) !== 1 || !hash_equals($trajectoryRevision, $trajectoryCalculated) || (int)($trajectory['generated_at_ts_ms'] ?? 0) !== (int)($plan['generated_at_ts_ms'] ?? 0) || (int)($trajectory['valid_from_ts_ms'] ?? 0) !== (int)($plan['valid_from_ts_ms'] ?? 0) || (int)($trajectory['horizon_end_ts_ms'] ?? 0) !== (int)($plan['horizon_end_ts_ms'] ?? 0) || (int)($trajectory['slot_duration_s'] ?? 0) !== (int)($plan['slot_duration_s'] ?? 0) || forecastTrajectoryCanonicalJson($trajectory['input_revisions'] ?? null) !== forecastTrajectoryCanonicalJson($plan['input_revisions'] ?? null) || ($binding['trajectory_revision'] ?? null) !== $trajectoryRevision) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_TRAJECTORY_NOT_AXIS_LIMITED');
    $durationS = (int)($plan['slot_duration_s'] ?? 0);
    $durationMs = $durationS * 1000;
    $validFromMs = (int)($plan['valid_from_ts_ms'] ?? 0);
    $horizonEndMs = (int)($plan['horizon_end_ts_ms'] ?? 0);
    if (($projection['schema_version'] ?? '') !== 'direct_marketing_selected_action_fallback_v1'
        || ($projection['active'] ?? null) !== true
        || ($projection['complete'] ?? null) !== true
        || ($projection['status'] ?? '') !== 'COMPLETE'
        || ($projection['consumer_scope'] ?? '') !== 'web_projection'
        || ($projection['control_effect'] ?? null) !== false
        || ($projection['runtime_effect_claim_allowed'] ?? null) !== false
        || ($projection['hardware_effect_claim_allowed'] ?? null) !== false
        || ($projection['candidate_effect_allowed'] ?? null) !== false
        || ($projection['reason_code'] ?? null) !== null
        || ($projection['plan_id'] ?? null) !== $planId
        || ($projection['plan_material_revision'] ?? null) !== $planId
        || (int)($projection['generated_at_ts_ms'] ?? 0) !== (int)($plan['generated_at_ts_ms'] ?? 0)
        || (int)($projection['valid_from_ts_ms'] ?? 0) !== $validFromMs
        || (int)($projection['valid_until_ts_ms'] ?? 0) !== (int)($plan['valid_until_ts_ms'] ?? 0)
        || (int)($projection['horizon_end_ts_ms'] ?? 0) !== $horizonEndMs
        || (int)($projection['slot_duration_s'] ?? 0) !== $durationS
        || forecastTrajectoryCanonicalJson($projection['input_revisions'] ?? null) !== forecastTrajectoryCanonicalJson($inputRevisions)
        || ($projection['input_revisions_revision'] ?? null) !== $inputRevisionsRevision
        || ($projection['trajectory_revision'] ?? null) !== $trajectoryRevision
        || ($binding['slot_axis_revision'] ?? null) !== ($projection['slot_axis_revision'] ?? null)
        || ($binding['action_axis_revision'] ?? null) !== ($projection['action_axis_revision'] ?? null)
        || !is_array($projection['slots'] ?? null)
        || $durationS <= 0 || $validFromMs <= 0 || $horizonEndMs <= $validFromMs) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_BINDING_INVALID');
    $sourceSlots = [];
    foreach (($plan['slots'] ?? []) as $sourceSlot) {
        if (!is_array($sourceSlot)) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_SLOT_INVALID');
        if ((int)($sourceSlot['end_ts_ms'] ?? 0) > $validFromMs) $sourceSlots[] = $sourceSlot;
    }
    if (count($sourceSlots) !== count($projection['slots'])) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_SLOT_BINDING_INVALID');
    $slotAxis = [];
    $actionAxis = [];
    $slotKeysExpected = ['action', 'action_horizon_revision', 'action_id', 'action_lineage_id', 'action_roles_revision', 'commands_allowed', 'economic_export_gate_revision', 'end_ts_ms', 'executable', 'gate_generation', 'gate_generation_id', 'gate_lineage_id', 'planned_w', 'pv_store_source_contract', 'segment_id', 'selected', 'slot_id', 'source_action', 'source_mode', 'source_projection_revision', 'source_window_revision', 'start_ts_ms', 'window_end_ts_ms', 'window_id', 'window_start_ts_ms'];
    $revisionFor = function($value) {
        $encoded = forecastTrajectoryCanonicalJson($value);
        return is_string($encoded) ? 'sha256:' . hash('sha256', $encoded) : null;
    };
    $normalizeMode = function($value) {
        $mode = strtolower(str_replace(['-', ' '], '_', trim((string)$value)));
        return in_array($mode, ['eco+', 'ecoplus'], true) ? 'eco_plus' : $mode;
    };
    $toTsMs = function($value) {
        if (!is_numeric($value)) return 0;
        $number = (float)$value;
        if (!is_finite($number) || $number <= 0.0) return 0;
        if ($number < 100000000000.0) $number *= 1000.0;
        return (int)round($number);
    };
    $directPlan = is_array($plan['direct_marketing'] ?? null) ? $plan['direct_marketing'] : [];
    $previousEnd = null;
    foreach ($projection['slots'] as $index => $slot) {
        if (!is_array($slot) || !isset($sourceSlots[$index])) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_SLOT_INVALID');
        $slotKeys = array_keys($slot);
        sort($slotKeys, SORT_STRING);
        if ($slotKeys !== $slotKeysExpected) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_SLOT_INVALID');
        $sourceSlot = $sourceSlots[$index];
        $slotId = (string)($slot['slot_id'] ?? '');
        $start = (int)($slot['start_ts_ms'] ?? 0);
        $end = (int)($slot['end_ts_ms'] ?? 0);
        $expectedSlotId = 'sha256:' . hash('sha256', forecastTrajectoryCanonicalJson(['plan_id' => $planId, 'start_ts_ms' => $start, 'end_ts_ms' => $end]));
        if (!hash_equals($expectedSlotId, $slotId) || $end - $start !== $durationMs || ($previousEnd === null && $start !== $validFromMs) || ($previousEnd !== null && $start !== $previousEnd) || (string)($sourceSlot['slot_id'] ?? '') !== $slotId || (int)($sourceSlot['start_ts_ms'] ?? 0) !== $start || (int)($sourceSlot['end_ts_ms'] ?? 0) !== $end) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_SLOT_BINDING_INVALID');
        $sourceProjection = is_array($sourceSlot['projection'] ?? null) ? $sourceSlot['projection'] : [];
        $sourceSelected = ($sourceProjection['direct_marketing_selected'] ?? null) === true;
        $sourceExecutable = ($sourceProjection['direct_marketing_plan_executable'] ?? null) === true;
        $sourceCommands = ($sourceProjection['direct_marketing_plan_commands_allowed'] ?? null) === true;
        $sidecarSelected = ($slot['selected'] ?? null) === true;
        if (($slot['executable'] ?? null) !== $sidecarSelected || ($slot['commands_allowed'] ?? null) !== $sidecarSelected || $sourceSelected !== $sidecarSelected || $sourceExecutable !== $sidecarSelected || $sourceCommands !== $sidecarSelected) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_ROLE_INCOMPLETE');
        if ($sidecarSelected) {
            $action = strtoupper(trim((string)($slot['action'] ?? '')));
            $plannedW = $slot['planned_w'] ?? null;
            $positivePowerAction = in_array($action, ['PV_STORE', 'ECONOMIC_EXPORT'], true);
            $zeroPowerAction = $action === 'CHARGE_BLOCK_WAIT';
            $mapping = [
                'action_id' => 'direct_marketing_plan_action_id', 'action_lineage_id' => 'direct_marketing_plan_action_lineage_id',
                'window_id' => 'direct_marketing_window_id', 'window_start_ts_ms' => 'direct_marketing_window_start_ts_ms',
                'window_end_ts_ms' => 'direct_marketing_window_end_ts_ms', 'segment_id' => 'direct_marketing_plan_segment_id',
                'source_action' => 'direct_marketing_plan_source_action', 'pv_store_source_contract' => 'direct_marketing_plan_pv_store_source_contract',
                'gate_lineage_id' => 'direct_marketing_gate_lineage_id', 'gate_generation' => 'direct_marketing_gate_generation',
                'gate_generation_id' => 'direct_marketing_gate_generation_id',
            ];
            if ((!$positivePowerAction && !$zeroPowerAction) || !is_numeric($plannedW) || !is_finite((float)$plannedW) || ($positivePowerAction && (float)$plannedW <= 0.0) || ($zeroPowerAction && abs((float)$plannedW) > 0.01) || ($sourceProjection['direct_marketing_plan_action'] ?? null) !== $action || abs(round((float)($sourceProjection['direct_marketing_planned_w'] ?? -1), 3) - (float)$plannedW) > 0.001) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_ACTION_BINDING_INVALID');
            foreach ($mapping as $sidecarKey => $sourceKey) {
                if (($slot[$sidecarKey] ?? null) !== ($sourceProjection[$sourceKey] ?? null)) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_ACTION_BINDING_INVALID');
            }
            $sourceWindows = [];
            foreach (($directPlan['windows'] ?? []) as $sourceWindow) {
                if (!is_array($sourceWindow)
                    || (string)($sourceWindow['action'] ?? '') !== (string)($slot['source_action'] ?? '')
                    || $toTsMs($sourceWindow['start_ts'] ?? null) !== (int)($slot['window_start_ts_ms'] ?? 0)
                    || $toTsMs($sourceWindow['end_ts'] ?? null) !== (int)($slot['window_end_ts_ms'] ?? 0)) continue;
                $sourceWindowId = trim((string)(
                    $action === 'ECONOMIC_EXPORT'
                        ? ($sourceWindow['export_plateau_id'] ?? '')
                        : ($sourceWindow['window_id'] ?? '')
                ));
                $projectedWindowId = trim((string)(
                    $action === 'ECONOMIC_EXPORT'
                        ? ($sourceProjection['direct_marketing_export_plateau_id'] ?? '')
                        : ($slot['window_id'] ?? '')
                ));
                if ($sourceWindowId === '' || $sourceWindowId !== $projectedWindowId
                    || ($sourceWindow['export_segment_id'] ?? null) !== ($sourceProjection['direct_marketing_export_segment_id'] ?? null)
                    || ($action === 'PV_STORE' && ($sourceWindow['pv_store_source_contract'] ?? null) !== ($slot['pv_store_source_contract'] ?? null))) continue;
                $sourceWindows[] = $sourceWindow;
            }
            if ($normalizeMode($slot['source_mode'] ?? null) !== $normalizeMode($sourceProjection['direct_marketing_plan_source_mode'] ?? null)
                || ($slot['source_projection_revision'] ?? null) !== $revisionFor($sourceProjection)
                || ($slot['action_horizon_revision'] ?? null) !== $revisionFor($sourceProjection['direct_marketing_action_horizon_contract'] ?? null)
                || ($slot['action_roles_revision'] ?? null) !== $revisionFor($sourceProjection['direct_marketing_action_roles'] ?? null)
                || ($slot['economic_export_gate_revision'] ?? null) !== (is_array($sourceProjection['direct_marketing_economic_export_gate'] ?? null) ? $revisionFor($sourceProjection['direct_marketing_economic_export_gate']) : null)
                || count($sourceWindows) !== 1
                || ($slot['source_window_revision'] ?? null) !== $revisionFor($sourceWindows[0])) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_ACTION_BINDING_INVALID');
            $actionAxis[] = array_intersect_key($slot, array_flip(['slot_id', 'action', 'planned_w', 'action_id', 'action_lineage_id', 'window_id', 'window_start_ts_ms', 'window_end_ts_ms', 'segment_id', 'source_action', 'source_mode', 'pv_store_source_contract', 'gate_lineage_id', 'gate_generation', 'gate_generation_id', 'source_window_revision', 'source_projection_revision', 'action_horizon_revision', 'action_roles_revision', 'economic_export_gate_revision']));
        } else {
            foreach (['action', 'planned_w', 'action_id', 'action_lineage_id', 'window_id', 'window_start_ts_ms', 'window_end_ts_ms', 'segment_id', 'source_action', 'source_mode', 'pv_store_source_contract', 'gate_lineage_id', 'gate_generation', 'gate_generation_id', 'source_window_revision', 'source_projection_revision', 'action_horizon_revision', 'action_roles_revision', 'economic_export_gate_revision'] as $key) {
                if (($slot[$key] ?? null) !== null) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_PASSIVE_SLOT_INVALID');
            }
        }
        $slotAxis[] = ['slot_id' => $slotId, 'start_ts_ms' => $start, 'end_ts_ms' => $end];
        $previousEnd = $end;
    }
    if ($previousEnd !== $horizonEndMs || ($projection['slot_axis_revision'] ?? null) !== $revisionFor($slotAxis) || ($projection['action_axis_revision'] ?? null) !== $revisionFor($actionAxis)) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_AXIS_INVALID');
    return $projection;
}

function forecastCanonicalDispatchSlotForTs($plan, $targetTsMs) {
    if (!forecastCanonicalDispatchPlanValid($plan)) return null;
    foreach ($plan['slots'] as $slot) {
        if (!is_array($slot)) continue;
        $start = isset($slot['start_ts_ms']) ? (float)$slot['start_ts_ms'] : 0.0;
        $end = isset($slot['end_ts_ms']) ? (float)$slot['end_ts_ms'] : 0.0;
        if ($start <= $targetTsMs && $targetTsMs < $end) return $slot;
    }
    return null;
}

function forecastCanonicalDispatchProjection($slot) {
    if (!is_array($slot)) return null;
    $projection = $slot['projection'] ?? null;
    return is_array($projection) ? $projection : null;
}

function forecastCanonicalAxisPoint($forecast, $axisName) {
    if (!is_array($forecast) || !is_array($forecast[$axisName] ?? null)) {
        return null;
    }
    $value = $forecast[$axisName]['point'] ?? null;
    if (!is_numeric($value) || !is_finite((float)$value) || (float)$value < 0.0) {
        return null;
    }
    return round((float)$value);
}

function forecastCanonicalAxisP50($forecast, $axisName) {
    if (!is_array($forecast) || !is_array($forecast[$axisName] ?? null)) {
        return null;
    }
    $quantileContract = is_array($forecast['quantile_contract'] ?? null)
        ? $forecast['quantile_contract']
        : [];
    $axisContract = is_array($quantileContract[$axisName] ?? null)
        ? $quantileContract[$axisName]
        : [];
    $axisValues = $forecast[$axisName];
    $controlAxes = is_array($quantileContract['control_axes'] ?? null)
        ? $quantileContract['control_axes']
        : [];
    $revisionPattern = '/^sha256:[0-9a-f]{64}$/';
    if (
        ($quantileContract['schema_version'] ?? '') !== 'forecast_quantile_contract_v1'
        || ($quantileContract['status'] ?? '') !== 'complete'
        || ($quantileContract['control_status'] ?? '') !== 'complete'
        || ($quantileContract['canonical_convention'] ?? '') !== 'cdf_non_exceedance'
        || !in_array($axisName, $controlAxes, true)
        || ($axisContract['schema_version'] ?? '') !== 'forecast_quantile_axis_v1'
        || ($axisContract['status'] ?? '') !== 'complete'
        || ($axisContract['control_status'] ?? '') !== 'complete'
        || ($axisContract['canonical_convention'] ?? '') !== 'cdf_non_exceedance'
        || ($axisContract['fresh'] ?? null) !== true
        || ($axisContract['explicit'] ?? null) !== true
        || ($axisContract['order_valid'] ?? null) !== true
        || ($axisContract['issue_time_bound'] ?? null) !== true
        || ($axisContract['lead_time_bound'] ?? null) !== true
        || ($axisContract['calibration_status'] ?? '') !== 'calibrated'
        || ($axisContract['calibration_revision_valid'] ?? null) !== true
        || ($axisContract['calibration_window_bound'] ?? null) !== true
        || ($axisContract['decision_use_allowed'] ?? null) !== true
        || !is_string($axisContract['source'] ?? null)
        || trim((string)$axisContract['source']) === ''
        || preg_match($revisionPattern, (string)($axisContract['revision'] ?? '')) !== 1
        || preg_match($revisionPattern, (string)($axisContract['calibration_revision'] ?? '')) !== 1
    ) {
        return null;
    }
    $values = [];
    foreach (['p10', 'p50', 'p90'] as $key) {
        $axisValue = $axisValues[$key] ?? null;
        $contractValue = $axisContract[$key] ?? null;
        if (
            !is_numeric($axisValue)
            || !is_numeric($contractValue)
            || !is_finite((float)$axisValue)
            || !is_finite((float)$contractValue)
            || (float)$axisValue < 0.0
            || (float)$axisValue !== (float)$contractValue
        ) {
            return null;
        }
        $values[$key] = (float)$axisValue;
    }
    if (!($values['p10'] <= $values['p50'] && $values['p50'] <= $values['p90'])) {
        return null;
    }
    return round($values['p50']);
}

function forecastReadDispatchGeneration($planFile, $runtimeFile, $maxAttempts = 2) {
    $attempts = max(1, (int)$maxAttempts);
    $lastPlan = [];
    $lastRuntime = [];
    $stablePlan = [];
    $stablePlanRawJson = null;
    $canonicalInputSeen = false;
    $reason = 'snapshot_missing';
    for ($attempt = 1; $attempt <= $attempts; $attempt++) {
        $planBeforeRawJson = file_exists($planFile) ? @file_get_contents($planFile) : null;
        $planBefore = is_string($planBeforeRawJson) ? @json_decode($planBeforeRawJson, true) : null;
        $runtime = file_exists($runtimeFile)
            ? @json_decode(file_get_contents($runtimeFile), true)
            : null;
        $planAfterRawJson = file_exists($planFile) ? @file_get_contents($planFile) : null;
        $planAfter = is_string($planAfterRawJson) ? @json_decode($planAfterRawJson, true) : null;
        $lastPlan = is_array($planAfter) ? $planAfter : [];
        $lastRuntime = is_array($runtime) ? $runtime : [];
        $canonicalInputSeen = $canonicalInputSeen
            || (is_array($planBefore) && (($planBefore['schema_version'] ?? '') === 'storage_dispatch_plan_v1'))
            || (is_array($planAfter) && (($planAfter['schema_version'] ?? '') === 'storage_dispatch_plan_v1'));
        if (!forecastCanonicalDispatchPlanValid($planBefore)
            || !forecastCanonicalDispatchPlanValid($planAfter)) {
            $reason = 'plan_invalid';
            continue;
        }
        $planIdBefore = (string)($planBefore['plan_id'] ?? '');
        $planIdAfter = (string)($planAfter['plan_id'] ?? '');
        if ($planIdBefore === '' || $planIdBefore !== $planIdAfter) {
            $reason = 'plan_changed_during_read';
            continue;
        }
        $stablePlan = $planAfter;
        $stablePlanRawJson = $planAfterRawJson;
        if (!is_array($runtime)
            || (($runtime['schema_version'] ?? '') !== 'storage_dispatch_runtime_v1')) {
            $reason = 'runtime_invalid';
            continue;
        }
        if (($runtime['plan_id'] ?? null) !== $planIdAfter) {
            $reason = 'runtime_plan_mismatch';
            continue;
        }
        $runtimeSlotId = (string)($runtime['slot_id'] ?? '');
        $slotMatched = false;
        foreach (($planAfter['slots'] ?? []) as $slot) {
            if (is_array($slot) && $runtimeSlotId !== '' && ($slot['slot_id'] ?? null) === $runtimeSlotId) {
                $slotMatched = true;
                break;
            }
        }
        if (!$slotMatched) {
            $reason = 'runtime_slot_mismatch';
            continue;
        }
        return [
            'plan' => $planAfter,
            'plan_raw_json' => $planAfterRawJson,
            'runtime' => $runtime,
            'coherent' => true,
            'plan_coherent' => true,
            'runtime_coherent' => true,
            'projection_status' => 'coherent',
            'canonical_input_seen' => true,
            'attempts' => $attempt,
            'reason' => 'ok',
            'plan_id' => $planIdAfter,
            'slot_id' => $runtimeSlotId,
        ];
    }
    return [
        'plan' => $stablePlan,
        'plan_raw_json' => $stablePlanRawJson,
        'runtime' => $lastRuntime,
        'coherent' => false,
        'plan_coherent' => !empty($stablePlan),
        'runtime_coherent' => false,
        'projection_status' => !empty($stablePlan) ? 'plan_only' : 'unavailable',
        'canonical_input_seen' => $canonicalInputSeen,
        'attempts' => $attempts,
        'reason' => $reason,
        'plan_id' => $stablePlan['plan_id'] ?? null,
        'slot_id' => $lastRuntime['slot_id'] ?? null,
    ];
}

function forecastDispatchGenerationDiagnostics($generation) {
    $reason = (string)($generation['reason'] ?? 'snapshot_missing');
    return [
        'schema_version' => 'forecast_storage_projection_status_v1',
        'status' => (string)($generation['projection_status'] ?? 'unavailable'),
        'reason_code' => strtoupper($reason),
        'plan_coherent' => !empty($generation['plan_coherent']),
        'runtime_coherent' => !empty($generation['runtime_coherent']),
        'plan_id' => $generation['plan_id'] ?? null,
        'runtime_slot_id' => $generation['slot_id'] ?? null,
        'attempts' => (int)($generation['attempts'] ?? 0),
    ];
}

function forecastDirectMarketingSlotState($projection, $runtime, $runtimeMatchesSlot, $requiresPrice) {
    $projection = is_array($projection) ? $projection : [];
    $runtime = is_array($runtime) ? $runtime : [];
    $runtimeCandidate = $runtimeMatchesSlot && is_array($runtime['candidate'] ?? null)
        ? $runtime['candidate']
        : [];
    $runtimeAction = strtoupper((string)($runtimeCandidate['action'] ?? ''));
    $runtimeSelectedAction = strtoupper((string)(
        $runtime['phase5']['selected_action']
        ?? $runtime['actual_manager_action']
        ?? ''
    ));
    $authorizedExportW = $runtimeMatchesSlot
        ? max(0.0, (float)($runtime['export_budget_w'] ?? 0.0))
        : 0.0;
    $authorizedChargeW = $runtimeMatchesSlot
        ? max(0.0, (float)($runtime['charge_budget_w'] ?? 0.0))
        : 0.0;
    $commandAction = in_array($runtimeAction, ['ECONOMIC_EXPORT', 'PV_STORE'], true);
    $executable = $requiresPrice
        && $runtimeMatchesSlot
        && !empty($runtime['executable'])
        && $commandAction;
    $commandsAllowed = $requiresPrice
        && $runtimeMatchesSlot
        && !empty($runtime['commands_allowed'])
        && $commandAction;
    $candidate = $requiresPrice && !empty($projection['direct_marketing_candidate']);
    $candidateW = $candidate
        ? max(0.0, (float)($projection['direct_marketing_candidate_w'] ?? 0.0))
        : 0.0;
    $selected = $requiresPrice && !empty($projection['direct_marketing_selected']);
    $planExecutable = $requiresPrice && !empty($projection['direct_marketing_plan_executable']);
    $planCommandsAllowed = $requiresPrice && !empty($projection['direct_marketing_plan_commands_allowed']);
    $plannedW = $selected && $planExecutable && $planCommandsAllowed
        ? max(0.0, (float)($projection['direct_marketing_planned_w'] ?? 0.0))
        : 0.0;
    $windowId = $projection['direct_marketing_window_id'] ?? null;
    $planAction = strtoupper((string)($projection['direct_marketing_plan_action'] ?? ''));
    $planActionId = $projection['direct_marketing_plan_action_id'] ?? null;
    $planSegmentId = $projection['direct_marketing_plan_segment_id'] ?? null;
    $planActionHorizon = is_array($projection['direct_marketing_action_horizon_contract'] ?? null)
        ? $projection['direct_marketing_action_horizon_contract']
        : [];
    $planEconomicGate = is_array($projection['direct_marketing_economic_export_gate'] ?? null)
        ? $projection['direct_marketing_economic_export_gate']
        : [];
    $planSelectionComplete = $selected
        && $planExecutable
        && $planCommandsAllowed
        && $plannedW > 0.0
        && in_array($planAction, ['ECONOMIC_EXPORT', 'PV_STORE'], true)
        && is_string($windowId)
        && trim($windowId) !== ''
        && is_string($planActionId)
        && trim($planActionId) !== ''
        && is_string($planSegmentId)
        && trim($planSegmentId) !== ''
        && (($planActionHorizon['schema_version'] ?? '') === 'storage_dispatch_action_horizon_v1')
        && (($planActionHorizon['action'] ?? '') === $planAction)
        && !empty($planActionHorizon['complete'])
        && (
            $planAction !== 'ECONOMIC_EXPORT'
            || (
                !empty($planEconomicGate['allowed'])
                && empty($planEconomicGate['blockers'])
            )
        );
    $runtimeClaimsAction = $runtimeMatchesSlot
        && $commandAction
        && (
            (!empty($runtime['selected']) && $runtimeSelectedAction === $runtimeAction)
            || !empty($runtime['executable'])
            || !empty($runtime['commands_allowed'])
            || $authorizedExportW > 0.0
            || $authorizedChargeW > 0.0
            || !empty($runtime['requested']['issued'])
            || !empty($runtime['phase5']['requested'])
            || !empty($runtime['phase5']['issued'])
            || !empty($runtime['phase5']['hardware_effect'])
        );
    $runtimeCandidatePowerW = is_numeric($runtimeCandidate['power_w'] ?? null)
        ? max(0.0, (float)$runtimeCandidate['power_w'])
        : null;
    $selectionInvariantValid = !$runtimeClaimsAction || (
        $planSelectionComplete
        && $runtimeAction === $planAction
        && (($runtimeCandidate['window_id'] ?? null) === $windowId)
        && (($runtimeCandidate['action_id'] ?? null) === $planActionId)
        && (($runtimeCandidate['segment_id'] ?? null) === $planSegmentId)
        && $runtimeCandidatePowerW !== null
        && abs($runtimeCandidatePowerW - $plannedW) <= 1.0
    );
    if (!$selectionInvariantValid) {
        $authorizedExportW = 0.0;
        $authorizedChargeW = 0.0;
        $executable = false;
        $commandsAllowed = false;
    }
    $active = $selected
        && $selectionInvariantValid
        && !empty($runtime['selected'])
        && $executable
        && $commandsAllowed
        && (
            ($runtimeAction === 'ECONOMIC_EXPORT' && $authorizedExportW > 0.0)
            || ($runtimeAction === 'PV_STORE' && $authorizedChargeW > 0.0)
        );
    return [
        'candidate' => $candidate,
        'candidate_w' => $candidateW,
        'candidate_action' => $candidate
            ? ($projection['direct_marketing_candidate_action'] ?? 'ECONOMIC_EXPORT')
            : null,
        'selected' => $selected,
        'plan_executable' => $planExecutable,
        'plan_commands_allowed' => $planCommandsAllowed,
        'planned_w' => $plannedW,
        'plan_action' => $planAction ?: null,
        'runtime_action' => $runtimeAction ?: null,
        'executable' => $executable,
        'commands_allowed' => $commandsAllowed,
        'authorized_export_w' => $active && $runtimeAction === 'ECONOMIC_EXPORT'
            ? $authorizedExportW
            : 0.0,
        'authorized_charge_w' => $active && $runtimeAction === 'PV_STORE'
            ? $authorizedChargeW
            : 0.0,
        'active' => $active,
        'hardware_effect' => $selectionInvariantValid
            && $runtimeMatchesSlot
            && (($runtime['phase5']['schema_version'] ?? '') === 'storage_dispatch_phase5_v1')
            && !empty($runtime['phase5']['hardware_effect']),
        'window_id' => $windowId,
        'export_segment_id' => $projection['direct_marketing_export_segment_id'] ?? null,
        'selection_invariant_valid' => $selectionInvariantValid,
        'selection_invariant_reason' => $selectionInvariantValid
            ? null
            : 'PLAN_RUNTIME_SELECTION_INVARIANT_VIOLATION',
        'block_reason' => !$selectionInvariantValid
            ? 'PLAN_RUNTIME_SELECTION_INVARIANT_VIOLATION'
            : ($runtimeMatchesSlot
            ? ($runtimeCandidate['block_reason_code']
                ?? $runtime['technical_block_reason_code']
                ?? $runtime['block_reason_code']
                ?? null)
            : ($projection['direct_marketing_block_reason'] ?? null)),
    ];
}

function forecastDirectMarketingPlannedChargeW($state) {
    $state = is_array($state) ? $state : [];
    if (
        !empty($state['active'])
        && strtoupper((string)($state['runtime_action'] ?? '')) === 'PV_STORE'
    ) {
        return max(0.0, (float)($state['authorized_charge_w'] ?? 0.0));
    }
    $fullySelectedPvStore = !empty($state['selected'])
        && !empty($state['plan_executable'])
        && !empty($state['plan_commands_allowed'])
        && strtoupper((string)($state['plan_action'] ?? '')) === 'PV_STORE';
    return $fullySelectedPvStore
        ? max(0.0, (float)($state['planned_w'] ?? 0.0))
        : 0.0;
}

function forecastLegacyStorageProjection($slot) {
    if (!is_array($slot)) return null;
    $pvW = isset($slot['pv_w']) ? (float)$slot['pv_w'] : null;
    $homeW = isset($slot['home_w']) ? (float)$slot['home_w'] : null;
    $wpW = isset($slot['wp_w']) ? (float)$slot['wp_w'] : null;
    $climateW = isset($slot['climate_w']) ? (float)$slot['climate_w'] : null;
    $batteryW = isset($slot['charge_w']) ? (float)$slot['charge_w'] : null;
    $gridW = ($pvW !== null && $homeW !== null && $batteryW !== null)
        ? $homeW + ($wpW ?? 0.0) + ($climateW ?? 0.0) + $batteryW - $pvW
        : null;
    $candidateW = isset($slot['predump_candidate_w'])
        ? max(0.0, (float)$slot['predump_candidate_w'])
        : max(0.0, (float)($slot['grid_dump_w'] ?? 0.0));
    return [
        'pv_w' => $pvW,
        'home_w' => $homeW,
        'wp_w' => $wpW,
        'climate_w' => $climateW,
        'wallbox_w' => 0.0,
        'battery_w' => $batteryW,
        'grid_w' => $gridW,
        'soc_pct' => isset($slot['soc']) ? (float)$slot['soc'] : null,
        'target_soc_pct' => null,
        'predump_candidate_w' => $candidateW,
        'predump_executable_w' => 0.0,
        'predump_status' => $candidateW > 0.0 ? 'candidate_only' : 'none',
        'direct_marketing_candidate' => false,
        'direct_marketing_candidate_w' => 0.0,
        'direct_marketing_selected' => false,
        'direct_marketing_executable' => false,
        'direct_marketing_commands_allowed' => false,
        'direct_marketing_plan_executable' => false,
        'direct_marketing_plan_commands_allowed' => false,
        'direct_marketing_block_reason' => null,
        'direct_marketing_export_w' => 0.0,
        'direct_marketing_planned_w' => 0.0,
        'direct_marketing_soc_pct' => null,
        'direct_marketing_window_id' => null,
        'direct_marketing_export_segment_id' => null,
        'direct_marketing_charge_w' => 0.0,
        'direct_marketing_action' => null,
        'direct_marketing_candidate_action' => null,
        'headroom_export_candidate_w' => 0.0,
        'headroom_export_reason' => null,
        'market_charge_w' => 0.0,
        'market_hold' => false,
        'market_action' => null,
        'source' => 'legacy_projection_no_resimulation'
    ];
}

function interpolateStorageTargetSoc($timeline, $targetTsMs) {
    if (empty($timeline) || !is_array($timeline)) {
        return null;
    }

    $points = [];
    foreach ($timeline as $point) {
        if (!isset($point['ts']) || !isset($point['soc'])) {
            continue;
        }
        $points[] = [
            'ts' => (int)$point['ts'],
            'soc' => (float)$point['soc']
        ];
    }
    if (!$points) {
        return null;
    }

    usort($points, function($a, $b) { return $a['ts'] <=> $b['ts']; });

    // Die Ladekurve ist tagesbezogen. Nicht versehentlich die heutige Zielkurve
    // als 72h-SOC für morgen/übermorgen fortschreiben.
    $targetDay = date('Y-m-d', (int)floor($targetTsMs / 1000));
    $planDay = date('Y-m-d', (int)floor($points[0]['ts'] / 1000));
    if ($targetDay !== $planDay) {
        return null;
    }

    $first = $points[0];
    $last = $points[count($points) - 1];
    if ($targetTsMs < $first['ts']) {
        return null;
    }
    if ($targetTsMs > $last['ts']) {
        return $last['soc'];
    }

    for ($i = 1; $i < count($points); $i++) {
        $prev = $points[$i - 1];
        $next = $points[$i];
        if ($targetTsMs <= $next['ts']) {
            $span = max(1, $next['ts'] - $prev['ts']);
            $ratio = ($targetTsMs - $prev['ts']) / $span;
            return $prev['soc'] + (($next['soc'] - $prev['soc']) * $ratio);
        }
    }

    return null;
}

function displayCurveConfigFloat($conf, $key, $default) {
    return isset($conf[$key]) ? parseNumericConfigValue($conf[$key], $default) : $default;
}

function displayCurveConfigBool($conf, $key, $default = false) {
    if (!isset($conf[$key])) {
        return (bool)$default;
    }
    if (is_bool($conf[$key])) {
        return $conf[$key];
    }
    return in_array(strtolower(trim((string)$conf[$key])), ['1', 'true', 'yes', 'on', 'ja', 'ein'], true);
}

function forecastModelValueW($slot, $newKey, $legacyKey) {
    $value = null;
    if (array_key_exists($newKey, $slot) && is_numeric($slot[$newKey])) {
        $value = (float)$slot[$newKey];
    } elseif (array_key_exists($legacyKey, $slot) && is_numeric($slot[$legacyKey])) {
        $value = (float)$slot[$legacyKey];
    }
    return $value === null ? null : round($value * 1000.0);
}

function forecastSlotPowerKw($slot) {
    if (!is_array($slot)) {
        return null;
    }
    foreach (['predicted_kwh', 'predicted_kw', 'pv_kw', 'pv_estimate'] as $key) {
        if (array_key_exists($key, $slot) && is_numeric($slot[$key])) {
            return max(0.0, (float)$slot[$key]);
        }
    }
    $modelValues = [];
    foreach (['m1_raw', 'm2_raw', 'm3_raw', 'm1', 'm2', 'm3'] as $key) {
        if (array_key_exists($key, $slot) && is_numeric($slot[$key])) {
            $modelValues[] = max(0.0, (float)$slot[$key]);
        }
    }
    if (!$modelValues) {
        return null;
    }
    return array_sum($modelValues) / count($modelValues);
}

function pvForecastDiagnosticsEnabled($conf) {
    $value = strtolower(trim((string)($conf['forecast_diagnostics_enable'] ?? '0')));
    return in_array($value, ['1', 'true', 'yes', 'on', 'ein'], true);
}

function forecastDiagnosticIssueContractValid($contract, $expectedRevision) {
    if (!is_array($contract)) return false;
    $revisionPattern = '/^sha256:[0-9a-f]{64}$/';
    $allowedStatuses = ['complete', 'EVIDENCE_LIMIT'];
    $statusFields = [
        'producer_issue_time_status', 'model_revision_status',
        'method_revision_status', 'postprocessing_revision_status',
        'topology_revision_status', 'source_composition_status',
        'target_slots_status',
    ];
    $statusRevisionFields = [
        'model_revision_status' => 'model_revision',
        'method_revision_status' => 'method_revision',
        'postprocessing_revision_status' => 'postprocessing_revision',
        'topology_revision_status' => 'topology_revision',
        'source_composition_status' => 'source_composition_revision',
    ];
    if (
        ($contract['schema_version'] ?? '') !== 'pv_forecast_issue_v1'
        || !in_array((string)($contract['status'] ?? ''), $allowedStatuses, true)
        || ($contract['producer'] ?? '') !== 'pv_forecast_service'
        || ($contract['producer_issue_time_basis'] ?? '') !== 'producer_output_generation_utc_v1'
        || ($contract['value_stage'] ?? '') !== 'displayed_postprocessed'
        || ($contract['distribution_type'] ?? '') !== 'deterministic_point'
        || !array_key_exists('declared_quantile', $contract)
        || !array_key_exists('quantile_convention', $contract)
        || $contract['declared_quantile'] !== null
        || $contract['quantile_convention'] !== null
        || ($contract['control_effect'] ?? null) !== false
        || ($contract['configuration_writes'] ?? null) !== false
        || ($contract['automatic_model_selection'] ?? null) !== false
        || ($contract['decision_use_allowed'] ?? null) !== false
    ) {
        return false;
    }
    foreach ($statusFields as $field) {
        if (!in_array((string)($contract[$field] ?? ''), $allowedStatuses, true)) {
            return false;
        }
        if ($field === 'producer_issue_time_status') {
            $value = $contract['producer_issued_at_utc_s'] ?? null;
            if (
                (($contract[$field] === 'complete') && (!is_int($value) || $value <= 0))
                || (($contract[$field] === 'EVIDENCE_LIMIT') && $value !== null)
            ) return false;
        } elseif (isset($statusRevisionFields[$field])) {
            $revisionField = $statusRevisionFields[$field];
            $value = $contract[$revisionField] ?? null;
            if (
                (($contract[$field] === 'complete') && preg_match($revisionPattern, (string)$value) !== 1)
                || (($contract[$field] === 'EVIDENCE_LIMIT') && $value !== null)
            ) return false;
        }
    }
    $issueId = $contract['issue_id'] ?? null;
    if ($issueId === null) {
        return ($contract['status'] ?? '') === 'EVIDENCE_LIMIT'
            && count(array_filter($statusFields, function($field) use ($contract) {
                return ($contract[$field] ?? '') !== 'EVIDENCE_LIMIT';
            })) === 0;
    }
    if (
        preg_match($revisionPattern, (string)$issueId) !== 1
        || ($contract['topology_revision_status'] ?? '') !== 'complete'
        || ($contract['topology_revision'] ?? '') !== $expectedRevision
        || !is_int($contract['target_slot_count'] ?? null)
        || (int)$contract['target_slot_count'] < 1
        || !is_int($contract['target_slot_start_utc_s'] ?? null)
        || !is_int($contract['target_slot_end_utc_s'] ?? null)
        || (int)$contract['target_slot_end_utc_s'] <= (int)$contract['target_slot_start_utc_s']
        || preg_match($revisionPattern, (string)($contract['target_slots_revision'] ?? '')) !== 1
        || !is_array($contract['source_composition'] ?? null)
        || ($contract['source_composition']['schema_version'] ?? '') !== 'pv_forecast_source_composition_v1'
        || !is_array($contract['source_composition']['sources'] ?? null)
    ) {
        return false;
    }
    $sources = $contract['source_composition']['sources'];
    if (count($sources) !== 3) return false;
    foreach ($sources as $source) {
        if (
            !is_array($source)
            || !in_array((string)($source['model_id'] ?? ''), ['m1', 'm2', 'm3'], true)
            || !is_string($source['provider'] ?? null)
            || trim((string)$source['provider']) === ''
            || !is_bool($source['configured'] ?? null)
            || !is_bool($source['available'] ?? null)
            || !is_bool($source['fresh'] ?? null)
            || preg_match($revisionPattern, (string)($source['model_input_revision'] ?? '')) !== 1
            || preg_match($revisionPattern, (string)($source['resource_input_revision'] ?? '')) !== 1
        ) return false;
    }
    $componentComplete = count(array_filter($statusFields, function($field) use ($contract) {
        return ($contract[$field] ?? '') !== 'complete';
    })) === 0;
    return ($contract['status'] ?? '') === ($componentComplete ? 'complete' : 'EVIDENCE_LIMIT');
}

function forecastDiagnosticContinuityProjection(
    $value,
    $expectedTopologyRevision,
    $expectedMethodRevision,
    $currentComparedSlots,
    $currentYieldRelevantDays
) {
    if (!is_array($value)) return null;
    $revisionPattern = '/^sha256:[0-9a-f]{64}$/';
    $retained = ($value['retained'] ?? null) === true;
    $mergedIntoCurrentMetrics = $value['merged_into_current_metrics'] ?? null;
    $status = (string)($value['status'] ?? '');
    $reason = (string)($value['reason'] ?? '');
    $currentTopology = $value['current_topology_revision'] ?? null;
    $currentMethod = $value['current_method_revision'] ?? null;
    $currentCompared = $value['current_compared_slots'] ?? null;
    $currentDays = $value['current_yield_relevant_days'] ?? null;
    if (
        ($value['schema_version'] ?? '') !== 'pv_forecast_evidence_continuity_v1'
        || $mergedIntoCurrentMetrics !== false
        || !is_bool($value['retained'] ?? null)
        || !is_int($currentCompared)
        || !is_int($currentDays)
        || $currentCompared < 0
        || $currentDays < 0
        || $currentDays > $currentCompared
        || $currentCompared !== $currentComparedSlots
        || $currentDays !== $currentYieldRelevantDays
        || $currentTopology !== $expectedTopologyRevision
        || $currentMethod !== $expectedMethodRevision
    ) {
        return null;
    }
    $previousSchema = $value['previous_summary_schema'] ?? null;
    $previousTopology = $value['previous_topology_revision'] ?? null;
    $previousMethod = $value['previous_method_revision'] ?? null;
    $previousCompared = $value['previous_compared_slots'] ?? null;
    $previousDays = $value['previous_yield_relevant_days'] ?? null;
    if (
        !is_int($previousCompared)
        || !is_int($previousDays)
        || $previousCompared < 0
        || $previousDays < 0
        || $previousDays > $previousCompared
    ) {
        return null;
    }
    if ($retained) {
        if (
            $status !== 'cohort_transition'
            || !in_array($reason, [
                'producer_contract_upgrade',
                'topology_revision_changed',
                'method_revision_changed',
                'current_contract_missing',
            ], true)
            || !in_array($previousSchema, [
                'pv_forecast_diagnostic_summary_v2',
                'pv_forecast_diagnostic_summary_v3',
                'pv_forecast_diagnostic_summary_v4',
            ], true)
            || ($previousTopology !== null
                && preg_match($revisionPattern, (string)$previousTopology) !== 1)
            || ($previousMethod !== null
                && preg_match($revisionPattern, (string)$previousMethod) !== 1)
            || ($previousSchema === 'pv_forecast_diagnostic_summary_v4'
                && $previousMethod === null)
        ) {
            return null;
        }
    } elseif (
        !in_array($status, ['continuous', 'EVIDENCE_LIMIT'], true)
        || !in_array($reason, ['no_prior_cohort', 'current_cohort_unavailable'], true)
        || $previousSchema !== null
        || $previousTopology !== null
        || $previousMethod !== null
        || $previousCompared !== 0
        || $previousDays !== 0
    ) {
        return null;
    }
    return [
        'schema_version' => 'pv_forecast_evidence_continuity_v1',
        'status' => $status,
        'reason' => $reason,
        'retained' => $retained,
        'merged_into_current_metrics' => false,
        'previous_summary_schema' => $previousSchema,
        'previous_topology_revision' => $previousTopology,
        'previous_method_revision' => $previousMethod,
        'previous_compared_slots' => $previousCompared,
        'previous_yield_relevant_days' => $previousDays,
        'current_topology_revision' => $currentTopology,
        'current_method_revision' => $currentMethod,
        'current_compared_slots' => $currentCompared,
        'current_yield_relevant_days' => $currentDays,
    ];
}

function loadPvForecastDiagnosticEvidence($currentTopologyRevision, $diagnosticsEnabled) {
    $metricLabels = [
        'trefferabweichung_wh' => 'Trefferabweichung',
        'richtungsversatz_wh' => 'Richtungsversatz',
        'quadratische_fehlerwurzel_wh' => 'Quadratische Fehlerwurzel (RMSE)',
        'persistenz_skill_score_pct' => 'Skill gegenüber Tagespersistenz',
        'energiegewichtete_gesamtabweichung_pct' => 'Energiegewichtete Gesamtabweichung',
        'vergleichsabdeckung_pct' => 'Vergleichsabdeckung',
    ];
    $leadTimeSpecs = [
        ['bucket_id' => 'lead_0_2h', 'label' => '0–2 h', 'min_minutes' => 0, 'max_minutes' => 120],
        ['bucket_id' => 'lead_2_6h', 'label' => '2–6 h', 'min_minutes' => 120, 'max_minutes' => 360],
        ['bucket_id' => 'lead_6_24h', 'label' => '6–24 h', 'min_minutes' => 360, 'max_minutes' => 1440],
        ['bucket_id' => 'lead_24_48h', 'label' => '24–48 h', 'min_minutes' => 1440, 'max_minutes' => 2880],
        ['bucket_id' => 'lead_48_72h', 'label' => '48–72 h', 'min_minutes' => 2880, 'max_minutes' => 4320],
    ];
    $forecastValueContract = [
        'signal' => 'pv_e3dc_dc',
        'source_contract' => 'resource_forecast_ensemble_v1',
        'value_stage' => 'displayed_postprocessed',
        'distribution_type' => 'deterministic_point',
        'declared_quantile' => null,
        'quantile_convention' => null,
        'p50_claim' => 'not_proven',
        'bias_sign_convention' => 'actual_minus_forecast_positive_underforecast',
        'decision_use_allowed' => false,
    ];
    $deterministicReference = [
        'status' => 'EVIDENCE_LIMIT',
        'method' => 'previous_day_same_utc_slot_observed_before_issue_v1',
        'reference' => 'previous_day_same_utc_slot_actual',
        'lookahead_guard' => 'reference_observed_at_or_before_producer_issue',
        'skill_score_definition' => '1_minus_rmse_forecast_div_rmse_reference',
        'compared_slots' => 0,
        'decision_use_allowed' => false,
    ];
    $probabilisticEvidence = [
        'status' => 'EVIDENCE_LIMIT',
        'reason' => 'explicit_quantiles_and_convention_missing',
        'required_quantile_convention' => 'cdf_or_exceedance_explicit',
        'empirical_quantile_coverage' => [],
        'interval_coverage_pct' => null,
        'mean_pinball_loss_wh' => null,
        'crps_wh' => null,
        'decision_use_allowed' => false,
    ];
    $observationQuality = [
        'observation_source_contract' => 'e3dc_db_history_day_15m_v1',
        'curtailment_exclusion_status' => 'EVIDENCE_LIMIT',
        'inverter_clipping_exclusion_status' => 'EVIDENCE_LIMIT',
        'external_shutdown_exclusion_status' => 'EVIDENCE_LIMIT',
        'availability_forecast_claim_allowed' => false,
        'decision_use_allowed' => false,
    ];
    $sourceDiagnostics = [
        [
            'signal' => 'pv_e3dc_dc',
            'status' => 'sammelt_evidenz',
            'forecast_source_contract' => 'resource_forecast_ensemble_v1',
            'observation_source_contract' => 'e3dc_db_history_day_15m_v1',
            'reason' => 'noch_keine_vergleichspaare',
        ],
        [
            'signal' => 'pv_external_ac',
            'status' => 'EVIDENCE_LIMIT',
            'forecast_source_contract' => 'resource_forecast_ensemble_v1',
            'observation_source_contract' => null,
            'reason' => 'validated_external_ac_history_missing',
        ],
        [
            'signal' => 'house_base_load',
            'status' => 'EVIDENCE_LIMIT',
            'forecast_source_contract' => null,
            'observation_source_contract' => null,
            'reason' => 'forecast_observation_pair_missing',
        ],
        [
            'signal' => 'heat_load',
            'status' => 'EVIDENCE_LIMIT',
            'forecast_source_contract' => null,
            'observation_source_contract' => null,
            'reason' => 'forecast_observation_pair_missing',
        ],
        [
            'signal' => 'wallbox_load',
            'status' => 'EVIDENCE_LIMIT',
            'forecast_source_contract' => null,
            'observation_source_contract' => null,
            'reason' => 'forecast_observation_pair_missing',
        ],
    ];
    $forecastIssueContract = [
        'schema_version' => 'pv_forecast_issue_v1',
        'status' => 'EVIDENCE_LIMIT',
        'issue_id' => null,
        'producer' => 'pv_forecast_service',
        'producer_issued_at_utc_s' => null,
        'producer_issue_time_basis' => 'producer_output_generation_utc_v1',
        'producer_issue_time_status' => 'EVIDENCE_LIMIT',
        'model_revision' => null,
        'model_revision_status' => 'EVIDENCE_LIMIT',
        'method_revision' => null,
        'method_revision_status' => 'EVIDENCE_LIMIT',
        'postprocessing_revision' => null,
        'postprocessing_revision_status' => 'EVIDENCE_LIMIT',
        'topology_revision' => null,
        'topology_revision_status' => 'EVIDENCE_LIMIT',
        'source_composition' => [
            'schema_version' => 'pv_forecast_source_composition_v1',
            'sources' => [],
        ],
        'source_composition_revision' => null,
        'source_composition_status' => 'EVIDENCE_LIMIT',
        'value_stage' => 'displayed_postprocessed',
        'distribution_type' => 'deterministic_point',
        'declared_quantile' => null,
        'quantile_convention' => null,
        'target_slot_count' => 0,
        'target_slot_start_utc_s' => null,
        'target_slot_end_utc_s' => null,
        'target_slots_revision' => null,
        'target_slots_status' => 'EVIDENCE_LIMIT',
        'control_effect' => false,
        'configuration_writes' => false,
        'automatic_model_selection' => false,
        'decision_use_allowed' => false,
    ];
    $evidenceLimits = [
        'producer_issue_time', 'external_model_revision',
        'producer_method_revision', 'postprocessing_revision',
        'topology_revision', 'source_composition', 'utc_target_slots',
        'probabilistic_quantiles', 'external_ac_observation_history',
        'curtailment_exclusion', 'inverter_clipping_exclusion',
        'external_shutdown_exclusion',
    ];
    $evidenceContinuity = [
        'schema_version' => 'pv_forecast_evidence_continuity_v1',
        'status' => 'EVIDENCE_LIMIT',
        'reason' => 'current_cohort_unavailable',
        'retained' => false,
        'merged_into_current_metrics' => false,
        'previous_summary_schema' => null,
        'previous_topology_revision' => null,
        'previous_method_revision' => null,
        'previous_compared_slots' => 0,
        'previous_yield_relevant_days' => 0,
        'current_topology_revision' => null,
        'current_method_revision' => null,
        'current_compared_slots' => 0,
        'current_yield_relevant_days' => 0,
    ];
    $fallback = [
        'schema_version' => 'pv_forecast_diagnostic_summary_v4',
        'status' => 'nicht_verfügbar',
        'available' => false,
        'reason' => 'diagnostic_summary_missing_or_invalid',
        'provisional' => true,
        'operation_mode' => 'read_only_diagnostic',
        'control_effect' => false,
        'configuration_writes' => false,
        'automatic_model_selection' => false,
        'decision_use_allowed' => false,
        'lead_time_basis' => 'producer_output_generation_to_slot_start_v1',
        'lead_time_evidence_status' => 'EVIDENCE_LIMIT',
        'producer_issue_time_status' => 'EVIDENCE_LIMIT',
        'model_revision_status' => 'EVIDENCE_LIMIT',
        'method_revision_status' => 'EVIDENCE_LIMIT',
        'postprocessing_revision_status' => 'EVIDENCE_LIMIT',
        'minimum_comparison_coverage_pct' => 100.0,
        'lead_time_buckets' => [],
        'persistence_compared_slots' => 0,
        'forecast_value_contract' => $forecastValueContract,
        'forecast_issue_contract' => $forecastIssueContract,
        'evidence_continuity' => $evidenceContinuity,
        'evidence_limits' => $evidenceLimits,
        'deterministic_reference' => $deterministicReference,
        'probabilistic_evidence' => $probabilisticEvidence,
        'observation_quality' => $observationQuality,
        'source_diagnostics' => $sourceDiagnostics,
        'metrics' => array_fill_keys(array_keys($metricLabels), null),
        'labels' => $metricLabels,
    ];
    if ($diagnosticsEnabled !== true) {
        return array_merge($fallback, [
            'status' => 'aus',
            'reason' => 'vom_nutzer_ausgeschaltet',
            'provisional' => false,
        ]);
    }
    $summaryPath = '/var/www/html/ramdisk/pv_forecast_diagnostic_summary.json';
    $maxBytes = 65536;
    $maxSummaryAgeSeconds = 36 * 3600;
    $expectedRevision = is_string($currentTopologyRevision)
        ? trim($currentTopologyRevision)
        : '';
    if (preg_match('/^sha256:[0-9a-f]{64}$/', $expectedRevision) === 1) {
        $fallback['evidence_continuity']['current_topology_revision'] = $expectedRevision;
    }
    if (
        !file_exists($summaryPath)
        || is_link($summaryPath)
        || !is_file($summaryPath)
        || !is_readable($summaryPath)
    ) {
        return $fallback;
    }

    $before = @lstat($summaryPath);
    if (
        !is_array($before)
        || (($before['mode'] ?? 0) & 0170000) !== 0100000
        || (int)($before['size'] ?? 0) < 2
        || (int)($before['size'] ?? 0) > $maxBytes
    ) {
        return $fallback;
    }

    $handle = null;
    try {
        $handle = @fopen($summaryPath, 'rb');
        if (!is_resource($handle)) {
            return $fallback;
        }
        $opened = @fstat($handle);
        if (
            !is_array($opened)
            || (int)($opened['dev'] ?? -1) !== (int)($before['dev'] ?? -2)
            || (int)($opened['ino'] ?? -1) !== (int)($before['ino'] ?? -2)
            || (($opened['mode'] ?? 0) & 0170000) !== 0100000
            || (int)($opened['size'] ?? 0) > $maxBytes
        ) {
            return $fallback;
        }
        $payloadJson = stream_get_contents($handle, $maxBytes + 1);
        if (
            !is_string($payloadJson)
            || $payloadJson === ''
            || strlen($payloadJson) > $maxBytes
        ) {
            return $fallback;
        }
        $payload = json_decode($payloadJson, true, 32);
        if (!is_array($payload)) {
            return $fallback;
        }
        if (!preg_match('/^sha256:[0-9a-f]{64}$/', $expectedRevision)) {
            $payloadRev = is_string($payload['topology_revision'] ?? null) ? trim($payload['topology_revision']) : '';
            if (preg_match('/^sha256:[0-9a-f]{64}$/', $payloadRev)) {
                $expectedRevision = $payloadRev;
                $fallback['evidence_continuity']['current_topology_revision'] = $expectedRevision;
            }
        }
        if (
            ($payload['schema_version'] ?? '') !== 'pv_forecast_diagnostic_summary_v4'
            || ($payload['operation_mode'] ?? '') !== 'read_only_diagnostic'
            || ($payload['control_effect'] ?? null) !== false
            || ($payload['configuration_writes'] ?? null) !== false
            || ($payload['automatic_model_selection'] ?? null) !== false
            || ($payload['decision_use_allowed'] ?? null) !== false
            || !(
                ($payload['topology_revision'] ?? null) === $expectedRevision
                || (
                    ($payload['topology_revision'] ?? null) === null
                    && ($payload['status'] ?? '') === 'nicht_verfügbar'
                    && ($payload['available'] ?? null) === false
                )
            )
            || !in_array(
                (string)($payload['status'] ?? ''),
                ['diagnostisch', 'vorläufig', 'nicht_verfügbar'],
                true
            )
            || (!is_bool($payload['available'] ?? null) && !in_array((string)($payload['status'] ?? ''), ['diagnostisch', 'vorläufig', 'nicht_verfügbar'], true))
            || !is_array($payload['metrics'] ?? null)
            || !is_array($payload['lead_time_buckets'] ?? null)
            || !is_array($payload['evidence_continuity'] ?? null)
            || ($payload['lead_time_basis'] ?? '') !== 'producer_output_generation_to_slot_start_v1'
            || !forecastDiagnosticIssueContractValid(
                $payload['forecast_issue_contract'] ?? null,
                $expectedRevision
            )
            || ($payload['producer_issue_time_status'] ?? '') !== ($payload['forecast_issue_contract']['producer_issue_time_status'] ?? '')
            || ($payload['model_revision_status'] ?? '') !== ($payload['forecast_issue_contract']['model_revision_status'] ?? '')
            || ($payload['method_revision_status'] ?? '') !== ($payload['forecast_issue_contract']['method_revision_status'] ?? '')
            || ($payload['postprocessing_revision_status'] ?? '') !== ($payload['forecast_issue_contract']['postprocessing_revision_status'] ?? '')
            || !is_array($payload['evidence_limits'] ?? null)
            || !is_numeric($payload['minimum_comparison_coverage_pct'] ?? null)
            || (float)$payload['minimum_comparison_coverage_pct'] !== 100.0
            || !is_array($payload['forecast_value_contract'] ?? null)
            || ($payload['forecast_value_contract']['signal'] ?? '') !== 'pv_e3dc_dc'
            || ($payload['forecast_value_contract']['source_contract'] ?? '') !== 'resource_forecast_ensemble_v1'
            || ($payload['forecast_value_contract']['value_stage'] ?? '') !== 'displayed_postprocessed'
            || ($payload['forecast_value_contract']['distribution_type'] ?? '') !== 'deterministic_point'
            || !array_key_exists('declared_quantile', $payload['forecast_value_contract'])
            || !array_key_exists('quantile_convention', $payload['forecast_value_contract'])
            || ($payload['forecast_value_contract']['declared_quantile'] ?? null) !== null
            || ($payload['forecast_value_contract']['quantile_convention'] ?? null) !== null
            || ($payload['forecast_value_contract']['p50_claim'] ?? '') !== 'not_proven'
            || ($payload['forecast_value_contract']['bias_sign_convention'] ?? '') !== 'actual_minus_forecast_positive_underforecast'
            || ($payload['forecast_value_contract']['decision_use_allowed'] ?? null) !== false
            || !is_array($payload['deterministic_reference'] ?? null)
            || ($payload['deterministic_reference']['method'] ?? '') !== 'previous_day_same_utc_slot_observed_before_issue_v1'
            || ($payload['deterministic_reference']['reference'] ?? '') !== 'previous_day_same_utc_slot_actual'
            || ($payload['deterministic_reference']['lookahead_guard'] ?? '') !== 'reference_observed_at_or_before_producer_issue'
            || ($payload['deterministic_reference']['skill_score_definition'] ?? '') !== '1_minus_rmse_forecast_div_rmse_reference'
            || ($payload['deterministic_reference']['decision_use_allowed'] ?? null) !== false
            || !is_array($payload['probabilistic_evidence'] ?? null)
            || ($payload['probabilistic_evidence']['status'] ?? '') !== 'EVIDENCE_LIMIT'
            || ($payload['probabilistic_evidence']['decision_use_allowed'] ?? null) !== false
            || !is_array($payload['observation_quality'] ?? null)
            || ($payload['observation_quality']['observation_source_contract'] ?? '') !== 'e3dc_db_history_day_15m_v1'
            || ($payload['observation_quality']['curtailment_exclusion_status'] ?? '') !== 'EVIDENCE_LIMIT'
            || ($payload['observation_quality']['inverter_clipping_exclusion_status'] ?? '') !== 'EVIDENCE_LIMIT'
            || ($payload['observation_quality']['external_shutdown_exclusion_status'] ?? '') !== 'EVIDENCE_LIMIT'
            || ($payload['observation_quality']['availability_forecast_claim_allowed'] ?? null) !== false
            || ($payload['observation_quality']['decision_use_allowed'] ?? null) !== false
        ) {
            return $fallback;
        }

        $metrics = [];
        foreach ($metricLabels as $key => $_label) {
            $value = $payload['metrics'][$key] ?? null;
            if ($value !== null && (!is_numeric($value) || !is_finite((float)$value))) {
                return $fallback;
            }
            $metrics[$key] = $value === null ? null : round((float)$value, 3);
        }
        $rawBucketsById = [];
        foreach ($payload['lead_time_buckets'] as $rawBucket) {
            if (!is_array($rawBucket)) {
                return $fallback;
            }
            $bucketId = (string)($rawBucket['bucket_id'] ?? '');
            if ($bucketId === '' || isset($rawBucketsById[$bucketId])) {
                return $fallback;
            }
            $rawBucketsById[$bucketId] = $rawBucket;
        }
        if (count($rawBucketsById) !== count($leadTimeSpecs)) {
            return $fallback;
        }
        $leadTimeBuckets = [];
        $allowedLeadStatuses = ['diagnostisch', 'vorläufig', 'EVIDENCE_LIMIT'];
        foreach ($leadTimeSpecs as $spec) {
            if (!isset($rawBucketsById[$spec['bucket_id']])) {
                return $fallback;
            }
            $rawBucket = $rawBucketsById[$spec['bucket_id']];
            $bucketMetrics = [];
            foreach ($metricLabels as $key => $_label) {
                $value = is_array($rawBucket['metrics'] ?? null)
                    ? ($rawBucket['metrics'][$key] ?? null)
                    : null;
                if ($value !== null && (!is_numeric($value) || !is_finite((float)$value))) {
                    return $fallback;
                }
                $bucketMetrics[$key] = $value === null ? null : round((float)$value, 3);
            }
            $bucketCounts = [];
            foreach ([
                'eligible_forecast_slots',
                'compared_slots',
                'yield_relevant_slots',
                'yield_relevant_days',
                'persistence_compared_slots',
            ] as $key) {
                $value = $rawBucket[$key] ?? 0;
                if (!is_numeric($value) || (int)$value < 0) {
                    return $fallback;
                }
                $bucketCounts[$key] = (int)$value;
            }
            if (
                $bucketCounts['compared_slots'] > $bucketCounts['eligible_forecast_slots']
                || $bucketCounts['yield_relevant_slots'] > $bucketCounts['compared_slots']
                || $bucketCounts['yield_relevant_days'] > $bucketCounts['yield_relevant_slots']
                || $bucketCounts['persistence_compared_slots'] > $bucketCounts['yield_relevant_slots']
            ) {
                return $fallback;
            }
            $leadValues = [];
            foreach ([
                'observed_lead_min_minutes',
                'observed_lead_max_minutes',
                'observed_lead_mean_minutes',
            ] as $key) {
                $value = $rawBucket[$key] ?? null;
                if (
                    $value !== null
                    && (
                        !is_numeric($value)
                        || !is_finite((float)$value)
                        || (float)$value < 0.0
                    )
                ) {
                    return $fallback;
                }
                $leadValues[$key] = $value === null ? null : round((float)$value, 3);
            }
            $bucketStatus = (string)($rawBucket['status'] ?? 'EVIDENCE_LIMIT');
            if (!in_array($bucketStatus, $allowedLeadStatuses, true)) {
                return $fallback;
            }
            $leadTimeBuckets[] = array_merge($spec, $bucketCounts, $leadValues, [
                'status' => $bucketStatus,
                'provisional' => ($rawBucket['provisional'] ?? null) === true,
                'provisional_reasons' => array_slice(array_values(array_filter(
                    $rawBucket['provisional_reasons'] ?? [],
                    'is_string'
                )), 0, 8),
                'metrics' => $bucketMetrics,
            ]);
        }
        $leadTimeStatus = (string)($payload['lead_time_evidence_status'] ?? 'EVIDENCE_LIMIT');
        if (!in_array($leadTimeStatus, $allowedLeadStatuses, true)) {
            return $fallback;
        }
        $nonnegativeIntegers = [];
        foreach ([
            'calculated_at_utc_s',
            'evaluation_window_days',
            'evaluation_delay_minutes',
            'minimum_relevant_slots',
            'minimum_relevant_days',
            'eligible_forecast_slots',
            'compared_slots',
            'yield_relevant_slots',
            'yield_relevant_days',
            'persistence_compared_slots',
        ] as $key) {
            $value = $payload[$key] ?? 0;
            if (!is_numeric($value) || (int)$value < 0) {
                return $fallback;
            }
            $nonnegativeIntegers[$key] = (int)$value;
        }
        if (
            $nonnegativeIntegers['compared_slots'] > $nonnegativeIntegers['eligible_forecast_slots']
            || $nonnegativeIntegers['yield_relevant_slots'] > $nonnegativeIntegers['compared_slots']
            || $nonnegativeIntegers['yield_relevant_days'] > $nonnegativeIntegers['yield_relevant_slots']
            || $nonnegativeIntegers['persistence_compared_slots'] > $nonnegativeIntegers['yield_relevant_slots']
        ) {
            return $fallback;
        }
        $rawReferenceCount = $payload['deterministic_reference']['compared_slots'] ?? null;
        $expectedReferenceStatus = $nonnegativeIntegers['persistence_compared_slots'] > 0
            ? 'diagnostisch'
            : 'EVIDENCE_LIMIT';
        if (
            !is_int($rawReferenceCount)
            || $rawReferenceCount !== $nonnegativeIntegers['persistence_compared_slots']
            || ($payload['deterministic_reference']['status'] ?? '') !== $expectedReferenceStatus
        ) {
            return $fallback;
        }
        $calculatedAt = $nonnegativeIntegers['calculated_at_utc_s'] ?? 0;
        if (
            $calculatedAt <= 0
            || $calculatedAt > (time() + 300)
            || (time() - $calculatedAt) > $maxSummaryAgeSeconds
        ) {
            return array_merge($fallback, ['reason' => 'zusammenfassung_veraltet']);
        }
        $payloadStatus = (string)$payload['status'];
        $payloadAvailable = $payload['available'] === true;
        if (
            ($payloadStatus === 'nicht_verfügbar' && $payloadAvailable)
            || ($payloadStatus !== 'nicht_verfügbar' && !$payloadAvailable)
        ) {
            return $fallback;
        }
        $diagnosticReason = (string)($payload['reason'] ?? '');
        if (
            ($payloadStatus === 'nicht_verfügbar' && $diagnosticReason === '')
            || ($diagnosticReason !== ''
                && preg_match('/^[a-z0-9_-]{1,80}$/', $diagnosticReason) !== 1)
        ) {
            return $fallback;
        }
        $forecastIssueContract = $payload['forecast_issue_contract'];
        $expectedMethodRevision =
            ($forecastIssueContract['method_revision_status'] ?? '') === 'complete'
                ? ($forecastIssueContract['method_revision'] ?? null)
                : null;
        $validatedContinuity = forecastDiagnosticContinuityProjection(
            $payload['evidence_continuity'],
            $payload['topology_revision'] ?? null,
            $expectedMethodRevision,
            $nonnegativeIntegers['compared_slots'],
            $nonnegativeIntegers['yield_relevant_days']
        );
        if ($validatedContinuity === null) {
            return $fallback;
        }
        if ($payloadStatus === 'diagnostisch') {
            $sourceDiagnostics[0]['status'] = 'diagnostisch';
            $sourceDiagnostics[0]['reason'] = 'ok';
        }
        $deterministicReference['status'] = $expectedReferenceStatus;
        $deterministicReference['compared_slots'] = $nonnegativeIntegers['persistence_compared_slots'];
        $allowedEvidenceLimits = [
            'producer_issue_time', 'external_model_revision',
            'producer_method_revision', 'postprocessing_revision',
            'topology_revision', 'source_composition', 'utc_target_slots',
            'probabilistic_quantiles', 'external_ac_observation_history',
            'curtailment_exclusion', 'inverter_clipping_exclusion',
            'external_shutdown_exclusion',
        ];
        $validatedEvidenceLimits = [];
        foreach ($payload['evidence_limits'] as $limit) {
            if (
                !is_string($limit)
                || !in_array($limit, $allowedEvidenceLimits, true)
                || in_array($limit, $validatedEvidenceLimits, true)
            ) {
                return $fallback;
            }
            $validatedEvidenceLimits[] = $limit;
        }
        return array_merge($fallback, $nonnegativeIntegers, [
            'topology_revision' => $expectedRevision,
            'status' => $payloadStatus,
            'available' => $payloadAvailable,
            'reason' => $diagnosticReason,
            'provisional' => ($payload['provisional'] ?? null) === true,
            'provisional_reasons' => array_slice(array_values(array_filter(
                $payload['provisional_reasons'] ?? [],
                'is_string'
            )), 0, 8),
            'lead_time_evidence_status' => $leadTimeStatus,
            'producer_issue_time_status' => $forecastIssueContract['producer_issue_time_status'],
            'model_revision_status' => $forecastIssueContract['model_revision_status'],
            'method_revision_status' => $forecastIssueContract['method_revision_status'],
            'postprocessing_revision_status' => $forecastIssueContract['postprocessing_revision_status'],
            'lead_time_buckets' => $leadTimeBuckets,
            'forecast_value_contract' => $forecastValueContract,
            'forecast_issue_contract' => $forecastIssueContract,
            'evidence_continuity' => $validatedContinuity,
            'evidence_limits' => $validatedEvidenceLimits,
            'deterministic_reference' => $deterministicReference,
            'probabilistic_evidence' => $probabilisticEvidence,
            'observation_quality' => $observationQuality,
            'source_diagnostics' => $sourceDiagnostics,
            'metrics' => $metrics,
            'labels' => $metricLabels,
        ]);
    } catch (Throwable $ignored) {
        return $fallback;
    } finally {
        if (is_resource($handle)) {
            fclose($handle);
        }
    }
}

function clampDisplaySoc($value) {
    return max(0.0, min(100.0, (float)$value));
}

function displayCurveLiveFloat($live, $key, $default = null) {
    if (!is_array($live) || !isset($live[$key]) || !is_numeric($live[$key])) {
        return $default;
    }
    return (float)$live[$key];
}

function displayCurveCapacityKwh($conf, $live) {
    $cfgCapacity = displayCurveConfigFloat($conf, 'speichergroesse', 0.0);
    if ($cfgCapacity > 0.1) {
        return $cfgCapacity;
    }
    foreach (['bat_total_usable_kwh', 'real_usable_capacity_kwh', 'usable_capacity_kwh', 'bat_usable_kwh', 'bat_capacity_kwh', 'bat_total_full_cap_kwh', 'bat_full_cap_kwh'] as $key) {
        $value = displayCurveLiveFloat($live, $key, 0.0);
        if ($value > 0.1) {
            $packCount = max(1.0, displayCurveLiveFloat($live, 'bat_total_dcb_count', displayCurveLiveFloat($live, 'bat_dcb_count', 1.0)));
            if (strpos($key, 'bat_total_') !== 0 && $packCount > 1.0 && $value < 5.0) {
                return $value * $packCount;
            }
            return $value;
        }
    }
    return 0.0;
}

function displayCurveEffectiveEpReservePct($conf) {
    $cfgPct = clampDisplaySoc(displayCurveConfigFloat($conf, 'ep_reserve_pct', 8.0));
    $livePath = '/var/www/html/ramdisk/live_data_py.json';
    $live = file_exists($livePath) ? @json_decode(file_get_contents($livePath), true) : null;
    if (!is_array($live)) {
        return $cfgPct;
    }

    $livePct = displayCurveLiveFloat($live, 'ep_reserve_effective_pct', null);
    if ($livePct === null) {
        $capacityKwh = displayCurveCapacityKwh($conf, $live);
        $capacityWh = $capacityKwh > 0.1 ? $capacityKwh * 1000.0 : 0.0;
        $energyWh = displayCurveLiveFloat($live, 'ep_reserve_energy_wh', null);
        $rawPct = displayCurveLiveFloat($live, 'ep_reserve_raw_pct', displayCurveLiveFloat($live, 'ep_reserve_pct', null));
        $maxEnergyWh = displayCurveLiveFloat($live, 'ep_reserve_max_energy_wh', null);
        if ($energyWh !== null && $capacityWh > 100.0) {
            $livePct = ($energyWh / $capacityWh) * 100.0;
        } elseif ($rawPct !== null && $maxEnergyWh !== null && $capacityWh > 100.0) {
            $livePct = (($maxEnergyWh * $rawPct / 100.0) / $capacityWh) * 100.0;
        } else {
            foreach (['ep_reserve_pct', 'notstrom_reserve', 'emergency_reserve_pct', 'reserve_percent'] as $key) {
                $candidate = displayCurveLiveFloat($live, $key, null);
                if ($candidate !== null) {
                    $livePct = $candidate;
                    break;
                }
            }
        }
    }

    return max($cfgPct, clampDisplaySoc($livePct ?? 0.0));
}

function repairShortMidnightPriceGaps(&$prices, $labels) {
    $n = count($prices);
    if ($n < 3) {
        return;
    }

    for ($i = 0; $i < $n; $i++) {
        if ($prices[$i] === null || abs((float)$prices[$i]) > 0.001) {
            continue;
        }

        $start = $i;
        while ($i + 1 < $n && $prices[$i + 1] !== null && abs((float)$prices[$i + 1]) <= 0.001) {
            $i++;
        }
        $end = $i;
        $len = $end - $start + 1;

        if ($len > 2) {
            continue;
        }

        $nearMidnight = false;
        for ($j = max(0, $start - 1); $j <= min($n - 1, $end + 1); $j++) {
            $label = $labels[$j] ?? '';
            if ($label === '23:45' || $label === '00:00' || $label === '00:15') {
                $nearMidnight = true;
                break;
            }
        }
        if (!$nearMidnight) {
            continue;
        }

        $prev = null;
        for ($j = $start - 1; $j >= 0; $j--) {
            if ($prices[$j] !== null && abs((float)$prices[$j]) > 0.001) {
                $prev = (float)$prices[$j];
                break;
            }
        }
        $next = null;
        for ($j = $end + 1; $j < $n; $j++) {
            if ($prices[$j] !== null && abs((float)$prices[$j]) > 0.001) {
                $next = (float)$prices[$j];
                break;
            }
        }

        if ($prev === null && $next === null) {
            continue;
        }
        $fill = $prev !== null && $next !== null ? round(($prev + $next) / 2.0, 2) : round(($prev ?? $next), 2);
        for ($j = $start; $j <= $end; $j++) {
            $prices[$j] = $fill;
        }
    }
}

function repeatPriceForSlot($segments, $targetTsMs) {
    if (empty($segments) || $targetTsMs <= 0) {
        return null;
    }
    $slotTs = (int)floor($targetTsMs / 1000);
    $slotMinute = ((int)date('G', $slotTs) * 60) + (int)date('i', $slotTs);
    foreach ($segments as $segment) {
        $matches = !empty($segment['wrap'])
            ? ($slotMinute >= $segment['start'] || $slotMinute < $segment['end'])
            : ($slotMinute >= $segment['start'] && $slotMinute < $segment['end']);
        if ($matches) {
            return round((float)$segment['price'], 2);
        }
    }
    return null;
}

function forecastDirectMarketingPriceCt($row, $fallback = null) {
    if (!is_array($row)) {
        return $fallback;
    }
    foreach (['direct_marketing_market_price_ct', 'direct_marketing_market_ct'] as $key) {
        if (array_key_exists($key, $row) && is_numeric($row[$key])) {
            return round((float)$row[$key], 5);
        }
    }
    foreach (['direct_marketing_marketprice', 'direct_marketing_market_price_eur_mwh'] as $key) {
        if (array_key_exists($key, $row) && is_numeric($row[$key])) {
            return round(((float)$row[$key]) / 10.0, 5);
        }
    }
    return $fallback;
}

function forecastMarketPriceCt($row, $fallback = null, $requireDirectMarketingPrice = false) {
    $directMarketingPrice = forecastDirectMarketingPriceCt($row, null);
    if ($directMarketingPrice !== null) {
        return $directMarketingPrice;
    }
    if ($requireDirectMarketingPrice || !is_array($row)) {
        return $fallback;
    }
    foreach (['market_price', 'marketprice'] as $key) {
        if (array_key_exists($key, $row) && is_numeric($row[$key])) {
            return round(((float)$row[$key]) / 10.0, 2);
        }
    }
    return $fallback;
}

$forecastRequiresDirectMarketingPrice = displayCurveConfigBool($conf, 'direct_marketing_enable', false);
$data['direct_marketing_enabled'] = $forecastRequiresDirectMarketingPrice;

function addRepeatPriceSegment(&$segments, &$uniquePrices, $startMinute, $endMinute, $price) {
    $priceVal = round((float)$price, 2);
    $start = max(0, min(1439, (int)$startMinute));
    $end = max(0, min(1440, (int)$endMinute));
    if ($end <= $start) {
        return;
    }
    $segments[] = [
        'start' => $start,
        'end' => $end,
        'price' => $priceVal,
        'wrap' => false
    ];
    $uniquePrices[number_format($priceVal, 2, '.', '')] = true;
}

function addStaticRepeatPriceSegments(&$segments, &$uniquePrices, $conf) {
    $basis = forecastWallboxConfigFloat($conf, 'strompreis_basis', 25.0);
    addRepeatPriceSegment($segments, $uniquePrices, 0, 1440, $basis);
}

function addOctopusHeatRepeatPriceSegments(&$segments, &$uniquePrices, $conf) {
    $basis = forecastWallboxConfigFloat($conf, 'strompreis_basis', 25.0);
    $cheap = forecastWallboxConfigFloat($conf, 'strompreis_cheap', 18.0);
    $uht = forecastWallboxConfigFloat($conf, 'strompreis_uht', 32.0);

    // Octopus Heat: LT 02-06 und 12-16, UHT 18-21, sonst HT.
    addRepeatPriceSegment($segments, $uniquePrices, 0, 120, $basis);
    addRepeatPriceSegment($segments, $uniquePrices, 120, 360, $cheap);
    addRepeatPriceSegment($segments, $uniquePrices, 360, 720, $basis);
    addRepeatPriceSegment($segments, $uniquePrices, 720, 960, $cheap);
    addRepeatPriceSegment($segments, $uniquePrices, 960, 1080, $basis);
    addRepeatPriceSegment($segments, $uniquePrices, 1080, 1260, $uht);
    addRepeatPriceSegment($segments, $uniquePrices, 1260, 1440, $basis);
}

// V4 Eco-Score einlesen
$ecoScores = null;
if ($v4_prices_available) {
    $ecoScores = @json_decode(file_get_contents($ecoScoreFile), true);
}
$repeatPriceSegments = [];
$repeatPriceUnique = [];
$repeatPricePattern = false;
$repeatPriceAllowed = in_array($stromtarifTyp, ['static', 'fix', 'fixed', 'flat', 'octopus_heat', 'special', 'spezial', 'special_tariff'], true);
if ($ecoScores && is_array($ecoScores)) {
    foreach ($ecoScores as $score) {
        if (!isset($score['start_timestamp']) || !isset($score['end_timestamp']) || !isset($score['billing_price'])) {
            continue;
        }
        $priceVal = round((float)$score['billing_price'], 2);
        $startTs = (int)floor(((float)$score['start_timestamp']) / 1000);
        $endTs = (int)floor(((float)$score['end_timestamp']) / 1000);
        if ($endTs <= $startTs) {
            continue;
        }
        $startMinute = ((int)date('G', $startTs) * 60) + (int)date('i', $startTs);
        $endMinute = ((int)date('G', $endTs) * 60) + (int)date('i', $endTs);
        $repeatPriceSegments[] = [
            'start' => $startMinute,
            'end' => $endMinute,
            'price' => $priceVal,
            'wrap' => $endMinute <= $startMinute
        ];
        $repeatPriceUnique[number_format($priceVal, 2, '.', '')] = true;
    }
}
if (in_array($stromtarifTyp, ['static', 'fix', 'fixed', 'flat'], true)) {
    addStaticRepeatPriceSegments($repeatPriceSegments, $repeatPriceUnique, $conf);
}
if ($stromtarifTyp === 'octopus_heat') {
    addOctopusHeatRepeatPriceSegments($repeatPriceSegments, $repeatPriceUnique, $conf);
}
// Fixtarife und wiederholende Tarife (z.B. Octopus Heat) haben wenige Preisstufen.
// Dynamische EPEX/Tibber-Reihen bleiben bewusst begrenzt, wenn der echte Horizont endet.
$repeatPricePattern = $repeatPriceAllowed && count($repeatPriceSegments) > 0 && count($repeatPriceUnique) <= 8;

$midnightMs = strtotime("today midnight") * 1000;
$forecastDischargeFloorSoc = displayCurveEffectiveEpReservePct($conf);
$data['forecast_reserve_floor_soc'] = round($forecastDischargeFloorSoc, 1);
$lastStorageCurveDay = null;
$lastStorageCurveSource = null;
$lastStorageCurveValue = null;

// Der Speicherplan und seine globalen Anzeigeverträge werden genau einmal pro
// Request gebunden. Insbesondere die kryptographisch gebundene DV-Trajektorie
// darf nicht für jeden 15-Minuten-Slot erneut vollständig validiert werden.
$stFile = '/var/www/html/ramdisk/storage_plan.json';
$runtimeFile = '/var/www/html/ramdisk/storage_dispatch_runtime.json';
$actionProjectionFile = '/var/www/html/ramdisk/storage_plan_action_projection.json';
$storageDispatchGeneration = forecastReadDispatchGeneration($stFile, $runtimeFile, 2);
$storageActionProjectionArtifact = forecastReadStoragePlanActionProjectionArtifact($actionProjectionFile);
$stData = $storageDispatchGeneration['plan'] ?? [];
$storagePlanParsed = is_array($stData['timeline'] ?? null) ? $stData['timeline'] : [];
$storagePlanMeta = is_array($stData) ? $stData : [];
$storageCanonicalPlanValid = forecastCanonicalDispatchPlanValid($storagePlanMeta);
$runtimeData = $storageDispatchGeneration['runtime'] ?? [];
$storageDispatchRuntime = is_array($runtimeData) ? $runtimeData : [];
$storageCanonicalSlotByStartMs = [];
$storageCanonicalSlotOriginMs = null;
$storageCanonicalSlotDurationMs = null;
if ($storageCanonicalPlanValid) {
    foreach (($storagePlanMeta['slots'] ?? []) as $slot) {
        if (!is_array($slot) || !isset($slot['start_ts_ms']) || !is_numeric($slot['start_ts_ms'])) {
            continue;
        }
        $slotStartMs = (int)$slot['start_ts_ms'];
        $slotStartKey = (string)$slotStartMs;
        if ($storageCanonicalSlotOriginMs === null || $slotStartMs < $storageCanonicalSlotOriginMs) {
            $storageCanonicalSlotOriginMs = $slotStartMs;
        }
        if ($storageCanonicalSlotDurationMs === null
            && isset($slot['end_ts_ms']) && is_numeric($slot['end_ts_ms'])) {
            $candidateDurationMs = (int)$slot['end_ts_ms'] - $slotStartMs;
            if ($candidateDurationMs > 0) {
                $storageCanonicalSlotDurationMs = $candidateDurationMs;
            }
        }
        // Bei einer unerwarteten doppelten Startzeit bleibt wie bisher der
        // erste gebundene Slot maßgeblich; es entsteht keine neue Projektion.
        if (!array_key_exists($slotStartKey, $storageCanonicalSlotByStartMs)) {
            $storageCanonicalSlotByStartMs[$slotStartKey] = $slot;
        }
    }
}
$storageLegacySlotByStartMs = [];
$storageLegacySlotOriginMs = null;
$storageLegacySlotDurationMs = 900000;
foreach ($storagePlanParsed as $slot) {
    if (!is_array($slot) || !isset($slot['ts']) || !is_numeric($slot['ts'])) {
        continue;
    }
    $slotStartMs = (int)$slot['ts'];
    $slotStartKey = (string)$slotStartMs;
    if ($storageLegacySlotOriginMs === null || $slotStartMs < $storageLegacySlotOriginMs) {
        $storageLegacySlotOriginMs = $slotStartMs;
    }
    if (!array_key_exists($slotStartKey, $storageLegacySlotByStartMs)) {
        $storageLegacySlotByStartMs[$slotStartKey] = $slot;
    }
}

$planCoherent = !empty($storageDispatchGeneration['plan_coherent']);
$planNowMs = microtime(true) * 1000.0;
$data['storage_plan_id'] = $storagePlanMeta['plan_id'] ?? null;
$data['storage_plan_generated_at'] = $storagePlanMeta['generated_at'] ?? null;
$data['storage_plan_schema_version'] = $storagePlanMeta['schema_version'] ?? null;
$data['storage_plan_valid_from'] = $storagePlanMeta['valid_from'] ?? null;
$data['storage_plan_valid_until'] = $storagePlanMeta['valid_until'] ?? null;
$data['storage_plan_horizon_end'] = $storagePlanMeta['horizon_end'] ?? null;
$data['storage_plan_fresh'] = $planCoherent
    && $storageCanonicalPlanValid
    && ((float)($storagePlanMeta['valid_from_ts_ms'] ?? 0) <= $planNowMs)
    && ($planNowMs < (float)($storagePlanMeta['valid_until_ts_ms'] ?? 0));
$data['storage_plan_meta'] = $storageCanonicalPlanValid
    ? [
        'schema_version' => $storagePlanMeta['schema_version'] ?? null,
        'plan_id' => $storagePlanMeta['plan_id'] ?? null,
        'generated_at' => $storagePlanMeta['generated_at'] ?? null,
        'valid_from' => $storagePlanMeta['valid_from'] ?? null,
        'valid_until' => $storagePlanMeta['valid_until'] ?? null,
        'input_revisions' => is_array($storagePlanMeta['input_revisions'] ?? null)
            ? $storagePlanMeta['input_revisions']
            : null,
        'planning_target_soc' => $storagePlanMeta['planning_target_soc'] ?? null,
        'target_soc' => $storagePlanMeta['target_soc'] ?? null,
        'pv_topology' => is_array($storagePlanMeta['pv_topology'] ?? null)
            ? $storagePlanMeta['pv_topology']
            : null,
        'headroom_topology' => is_array($storagePlanMeta['headroom_topology'] ?? null)
            ? $storagePlanMeta['headroom_topology']
            : null,
        'consumption_forecast' => is_array($storagePlanMeta['consumption_forecast'] ?? null)
            ? $storagePlanMeta['consumption_forecast']
            : null,
        'direct_marketing' => (
            $forecastRequiresDirectMarketingPrice
            && $planCoherent
            && is_array($storagePlanMeta['direct_marketing'] ?? null)
        ) ? $storagePlanMeta['direct_marketing'] : null,
    ]
    : null;
$data['direct_marketing'] = is_array($data['storage_plan_meta']['direct_marketing'] ?? null)
    ? $data['storage_plan_meta']['direct_marketing']
    : null;
$directMarketingSocProjection = is_array($storagePlanMeta['planner']['direct_marketing_projection'] ?? null)
    ? $storagePlanMeta['planner']['direct_marketing_projection']
    : [];
$data['direct_marketing_soc_projection'] = [
    'schema_version' => $directMarketingSocProjection['schema_version'] ?? null,
    'complete' => !empty($directMarketingSocProjection['complete']) && $planCoherent,
    'plan_id' => $storagePlanMeta['plan_id'] ?? null,
    'action_horizon_end_ts_ms' => $directMarketingSocProjection['action_horizon_end_ts_ms'] ?? null,
    'projection_horizon_end_ts_ms' => $directMarketingSocProjection['projection_horizon_end_ts_ms'] ?? null,
    'soc_source' => $directMarketingSocProjection['soc_source'] ?? null,
    'reason_code' => !empty($directMarketingSocProjection['complete'])
        ? null
        : ($directMarketingSocProjection['status'] ?? 'DIRECT_MARKETING_SOC_PROJECTION_INCOMPLETE'),
];
$data['direct_marketing_trajectory'] = forecastDirectMarketingTrajectoryForDisplay(
    $storagePlanMeta,
    $forecastRequiresDirectMarketingPrice,
    $planCoherent
);
$data['direct_marketing_selected_action_fallback'] = forecastDirectMarketingSelectedActionFallbackForDisplay(
    $storagePlanMeta,
    $forecastRequiresDirectMarketingPrice,
    $planCoherent,
    $storageDispatchGeneration['plan_raw_json'] ?? null,
    $storageActionProjectionArtifact
);
$data['storage_dispatch_runtime'] = $storageDispatchRuntime;
$storageDispatchDiagnostics = forecastDispatchGenerationDiagnostics($storageDispatchGeneration);
$data['storage_dispatch_generation'] = is_array($storageDispatchDiagnostics)
    ? $storageDispatchDiagnostics
    : [];
$data['storage_projection_status'] = array_merge(
    $data['storage_dispatch_generation'],
    [
        'plan_fresh' => !empty($data['storage_plan_fresh']),
        'soc_curve_current' => $planCoherent && !empty($data['storage_plan_fresh']),
    ]
);

foreach ($merged as $key => &$row) {
    $count++;
    if ($count % $step !== 0 && $count !== $totalLines) continue;


    $tRaw = $row['tRaw'];
    if ($tRaw > 10000) {
        // GMT Timestamp (z.B. aus 'Simulation' Tagessprung) auf 15-Min runden
        $m = (int)date('i', (int)$tRaw);
        $mRound = round($m / 15) * 15;
        $tRounded = (int)$tRaw + ($mRound - $m) * 60;
        $label = date('H:i', $tRounded);
    } else {
        // Dezimal-Stunden (GMT) runden auf nächstes 15-Minuten Raster
        $offsetHours = (int)date('Z') / 3600;
        $tLocal = $tRaw + $offsetHours;

        $totalMinutes = round($tLocal * 60);
        $roundedMinutes = round($totalMinutes / 15) * 15;

        $h = (int)floor($roundedMinutes / 60);
        $m = (int)($roundedMinutes % 60);
        if ($h < 0) { $h += 24; }

        $label = sprintf('%02d:%02d', $h % 24, $m);
    }

    $data['labels'][] = $label;

    // V4 Unix Timestamp für KI Arrays berechnen
    if ($tRaw > 10000) {
        $targetTsMs = ((int)$tRaw) * 1000;
    } else {
        $midnightGmt = strtotime(gmdate("Y-m-d", $sourceTs) . " 00:00:00 UTC");
        $targetTsMs = ($midnightGmt + ($tRaw * 3600)) * 1000;
    }
    $data['timestamps'][] = (int)$targetTsMs;

    // V4 Eco-Score einlesen
    $finalPrice = isset($row['price']) ? round($row['price'], 2) : 0;
    $finalMarketPrice = forecastMarketPriceCt($row, null, $forecastRequiresDirectMarketingPrice);
    $finalScore = null;
    $hasV4Price = false;
    $maxEcoTs = 0;

    if ($ecoScores && is_array($ecoScores)) {
        foreach ($ecoScores as $score) {
            if ($score['end_timestamp'] > $maxEcoTs) { $maxEcoTs = $score['end_timestamp']; }
            if ($targetTsMs >= $score['start_timestamp'] && $targetTsMs < $score['end_timestamp']) {
                $finalPrice = round($score['billing_price'], 2);
                $finalMarketPrice = forecastMarketPriceCt($score, null, $forecastRequiresDirectMarketingPrice);
                $finalScore = round($score['optimization_score']);
                $hasV4Price = true;
                break;
            }
        }
    }

    if (!$hasV4Price && $repeatPricePattern && ($finalPrice === null || abs((float)$finalPrice) <= 0.001)) {
        $repeatPrice = repeatPriceForSlot($repeatPriceSegments, $targetTsMs);
        if ($repeatPrice !== null) {
            $finalPrice = $repeatPrice;
        }
    }

    // V4: Wenn der Preis nach dem Awattar-Horizont komplett fehlt (0.00 am nächsten/übernächsten Tag),
    // dann setze ihn im Chart auf null (Linie bricht dort elegant ab, anstatt hart auf 0 ct zu fallen).
    if (!$hasV4Price && $finalPrice == 0 && $targetTsMs > $maxEcoTs && $targetTsMs > $midnightMs) {
        $finalPrice = null;
        $finalMarketPrice = null;
        if ($repeatPricePattern) {
            $repeatPrice = repeatPriceForSlot($repeatPriceSegments, $targetTsMs);
            if ($repeatPrice !== null) {
                $finalPrice = $repeatPrice;
            }
        }
    }

    $data['price'][] = $finalPrice;
    $data['market_price'][] = $finalMarketPrice;
    $data['eco_score'][] = $finalScore;

    // NEU: Wettermodelle einlesen
    $pv_m1 = null; $pv_m2 = null; $pv_m3 = null; $pv_ensemble = null; $pv_history = null;
    static $pvForecastsParsed = null;
    if ($pvForecastsParsed === null) {
        $pvForecastFile = '/var/www/html/ramdisk/pv_forecast.json';
        $pvForecastsParsed = file_exists($pvForecastFile) ? @json_decode(file_get_contents($pvForecastFile), true) : false;
    }
    // History-Buffer: Was hat das KI-Ensemble für vergangene Slots vorhergesagt?
    static $pvHistoryParsed = null;
    if ($pvHistoryParsed === null) {
        $histFile = '/var/www/html/ramdisk/pv_forecast_history.json';
        $pvHistoryParsed = file_exists($histFile) ? @json_decode(file_get_contents($histFile), true) : false;
    }

    if ($pvForecastsParsed) {
        foreach ($pvForecastsParsed as $pvF) {
            if ($targetTsMs >= $pvF['start_timestamp'] && $targetTsMs < $pvF['end_timestamp']) {
                $pv_m1 = forecastModelValueW($pvF, 'm1_raw', 'm1');
                $pv_m2 = forecastModelValueW($pvF, 'm2_raw', 'm2');
                $pv_m3 = forecastModelValueW($pvF, 'm3_raw', 'm3');
                $slotKw = forecastSlotPowerKw($pvF);
                $pv_ensemble = $slotKw !== null ? round($slotKw * 1000.0) : null;
                break;
            }
        }
    }
    // Vergangenheits-Overlay: KI-Prognose für abgelaufene Slots
    if ($pvHistoryParsed && $targetTsMs < (time() * 1000)) {
        foreach ($pvHistoryParsed as $pvH) {
            if ($targetTsMs >= $pvH['start_timestamp'] && $targetTsMs < $pvH['end_timestamp']) {
                $historyKw = forecastSlotPowerKw($pvH);
                $pv_history = $historyKw !== null ? $historyKw * 1000.0 : null;
                break;
            }
        }
    }
    $data['pv_m1'][]      = $pv_m1 !== null ? (int)$pv_m1 : null;
    $data['pv_m2'][]      = $pv_m2 !== null ? (int)$pv_m2 : null;
    $data['pv_m3'][]      = $pv_m3 !== null ? (int)$pv_m3 : null;
    $data['pv_ensemble'][]= $pv_ensemble !== null ? (int)$pv_ensemble : null;
    $data['pv_history'][] = $pv_history !== null ? round($pv_history) : null;


    // NEU: Machine Learning Vorhersage (Haus/WP/Klima) einlesen
    static $mlPredictionParsed = null;
    if ($mlPredictionParsed === null) {
        $mlFile = '/var/www/html/ramdisk/ml_prediction.json';
        $mlData = file_exists($mlFile) ? @json_decode(file_get_contents($mlFile), true) : false;
        $mlPredictionParsed = $mlData && isset($mlData['timeline']) ? $mlData['timeline'] : [];
    }

    $ml_home = null; $ml_wp = null; $ml_climate = null;
    if (!empty($mlPredictionParsed)) {
        foreach ($mlPredictionParsed as $mlF) {
            if ($targetTsMs >= $mlF['start_timestamp'] && $targetTsMs < $mlF['end_timestamp']) {
                $ml_home = $mlF['home_kwh'] !== null ? $mlF['home_kwh'] * 1000.0 : null;
                $ml_wp = $mlF['wp_kwh'] !== null ? $mlF['wp_kwh'] * 1000.0 : null;
                $ml_climate = (isset($mlF['climate_kwh']) && $mlF['climate_kwh'] !== null) ? $mlF['climate_kwh'] * 1000.0 : null;
                break;
            }
        }
    }

    // Slotwerte werden nur noch aus dem einmal gebundenen Plan übernommen.
    // Die Startzeit-Indizes vermeiden lineare Vollsuchen für jeden Chartpunkt.
    $slotStartKey = (string)((int)$targetTsMs);
    $canonicalSlot = $planCoherent ? ($storageCanonicalSlotByStartMs[$slotStartKey] ?? null) : null;
    if ($planCoherent && !is_array($canonicalSlot)
        && $storageCanonicalSlotOriginMs !== null
        && $storageCanonicalSlotDurationMs !== null
        && (int)$targetTsMs >= $storageCanonicalSlotOriginMs) {
        $slotOffset = intdiv(
            (int)$targetTsMs - $storageCanonicalSlotOriginMs,
            $storageCanonicalSlotDurationMs
        );
        $derivedSlotStartMs = $storageCanonicalSlotOriginMs + ($slotOffset * $storageCanonicalSlotDurationMs);
        $canonicalSlot = $storageCanonicalSlotByStartMs[(string)$derivedSlotStartMs] ?? null;
    }
    $storageProjection = forecastCanonicalDispatchProjection($canonicalSlot);
    $st_bat = null; $st_soc = null; $st_home = null; $st_wp = null; $st_climate = null;
    $st_predump_candidate_w = 0.0; $st_predump_executable_w = 0.0; $st_predump_status = 'none';
    $allowLegacyProjection = empty($storageDispatchGeneration['canonical_input_seen']);
    if (!is_array($storageProjection) && $allowLegacyProjection) {
        $legacySlot = $storageLegacySlotByStartMs[$slotStartKey] ?? null;
        if (!is_array($legacySlot)
            && $storageLegacySlotOriginMs !== null
            && (int)$targetTsMs >= $storageLegacySlotOriginMs) {
            $slotOffset = intdiv(
                (int)$targetTsMs - $storageLegacySlotOriginMs,
                $storageLegacySlotDurationMs
            );
            $derivedSlotStartMs = $storageLegacySlotOriginMs + ($slotOffset * $storageLegacySlotDurationMs);
            $legacySlot = $storageLegacySlotByStartMs[(string)$derivedSlotStartMs] ?? null;
        }
        $storageProjection = forecastLegacyStorageProjection($legacySlot);
    }
    if (is_array($storageProjection)) {
        $st_bat = $storageProjection['battery_w'] ?? null;
        $st_soc = $storageProjection['soc_pct'] ?? null;
        $st_home = $storageProjection['home_w'] ?? null;
        $st_wp = $storageProjection['wp_w'] ?? ($storageProjection['heat_w'] ?? null);
        $st_climate = $storageProjection['climate_w'] ?? null;
        $st_predump_candidate_w = max(0.0, (float)($storageProjection['predump_candidate_w'] ?? 0.0));
        $st_predump_executable_w = max(0.0, (float)($storageProjection['predump_executable_w'] ?? 0.0));
        $st_predump_status = (string)($storageProjection['predump_status'] ?? 'none');
    }
    $data['storage_slot_id'][] = is_array($canonicalSlot) ? ($canonicalSlot['slot_id'] ?? null) : null;
    $canonicalForecast = is_array($canonicalSlot['forecast_w'] ?? null)
        ? $canonicalSlot['forecast_w']
        : [];
    $canonicalPvTopology = is_array($canonicalForecast['topology'] ?? null)
        ? $canonicalForecast['topology']
        : [];
    $canonicalHeadroom = is_array($canonicalSlot['headroom_wh'] ?? null)
        ? $canonicalSlot['headroom_wh']
        : [];
    $data['pv_e3dc_dc'][] = forecastCanonicalAxisPoint(
        $canonicalForecast,
        'e3dc_dc_pv'
    );
    $data['pv_external_ac'][] = forecastCanonicalAxisPoint(
        $canonicalForecast,
        'external_ac_pv'
    );
    $data['pv_e3dc_dc_p50'][] = forecastCanonicalAxisP50(
        $canonicalForecast,
        'e3dc_dc_pv'
    );
    $data['pv_external_ac_p50'][] = forecastCanonicalAxisP50(
        $canonicalForecast,
        'external_ac_pv'
    );
    $data['pv_topology_status'][] = $canonicalPvTopology['status'] ?? 'topology_unbound';
    $data['pv_topology_reason'][] = $canonicalPvTopology['reason'] ?? 'CANONICAL_SLOT_TOPOLOGY_MISSING';
    $data['pv_topology_revision'][] = $canonicalPvTopology['revision'] ?? null;
    $data['pv_topology_source'][] = $canonicalPvTopology['source'] ?? null;
    $data['pv_topology_quality'][] = $canonicalPvTopology['quality'] ?? null;
    $data['pv_resource_projection_status'][] = $canonicalPvTopology['resource_projection_status'] ?? null;
    $data['pv_resource_projection_reason'][] = $canonicalPvTopology['resource_projection_reason'] ?? null;
    $data['headroom_dc_pressure_wh'][] = isset($canonicalHeadroom['dc_pressure'])
        ? round((float)$canonicalHeadroom['dc_pressure'], 3)
        : 0.0;
    $data['headroom_pcc_pressure_wh'][] = isset($canonicalHeadroom['pcc_pressure'])
        ? round((float)$canonicalHeadroom['pcc_pressure'], 3)
        : 0.0;
    $data['headroom_combined_pressure_wh'][] = isset($canonicalHeadroom['combined_pressure'])
        ? round((float)$canonicalHeadroom['combined_pressure'], 3)
        : 0.0;
    $data['headroom_deadline_ts'][] = $canonicalHeadroom['deadline_ts_ms'] ?? null;

    $marketGridCharge = is_array($storageProjection)
        && (float)($storageProjection['market_charge_w'] ?? 0.0) > 0.0;

    // V4 KI-Arrays haben Vorrang vor Legacy-Werten (die durch das leere $lines-Array ohnehin leer sind).
    // Prioritaet: pv_ensemble > storage_plan > Leerwert
    if ($pv_ensemble !== null) $row['pv'] = $pv_ensemble;
    // Home, WP und Klima: ml_prediction hat Vorrang (genauere Vorhersage), storage_plan als Fallback für >50h
    if ($ml_home !== null) {
        $row['home'] = $ml_home;
    } elseif ($st_home !== null) {
        $row['home'] = $st_home;  // Fallback: storage_sim Wert für Stunden jenseits ml_prediction
    }
    if ($ml_wp !== null) {
        $row['wp'] = $ml_wp;
    } elseif ($st_wp !== null) {
        $row['wp'] = $st_wp;
    }
    if ($ml_climate !== null) {
        $row['climate'] = $ml_climate;
    } elseif ($st_climate !== null) {
        $row['climate'] = $st_climate;
    }
    if ($st_bat !== null) {
        $allowEbaGridCharging = $marketGridCharge || (isset($row['bat']) && $row['bat'] > 0 && $finalPrice !== null && $finalPrice <= 15.0);
        if (!$allowEbaGridCharging) {
            $row['bat'] = $st_bat;
        }
    }

    // Native Ladefenster als geplante Last zeigen. Live-Regelung bleibt dynamisch,
    // aber die Ladeplanung muss die reservierten WB1/WB2-Slots im Grid-Calc sehen.
    $plannedSlotTs = (int)floor(((int)floor($targetTsMs / 1000)) / 900) * 900;
    $plannedWallboxPower = $plannedWallboxPowerByTs[$plannedSlotTs] ?? null;
    $row['wb'] = $plannedWallboxPower ? (float)($plannedWallboxPower[1] ?? 0.0) : 0.0;
    $row['wb2'] = $plannedWallboxPower ? (float)($plannedWallboxPower[2] ?? 0.0) : 0.0;

    // Ein kanonischer Slot ist die einzige fachliche Forecastquelle. Externe
    // Dateien dürfen für Legacydarstellung helfen, aber keine Slotwerte einer
    // plan_id überschreiben oder neu berechnen.
    if (is_array($canonicalSlot) && is_array($storageProjection)) {
        $row['pv'] = $storageProjection['pv_w'] ?? null;
        $row['home'] = $storageProjection['home_w'] ?? null;
        $row['wp'] = $storageProjection['wp_w'] ?? null;
        $row['climate'] = $storageProjection['climate_w'] ?? null;
        $row['wb'] = $storageProjection['wallbox_w'] ?? 0.0;
        $row['wb2'] = 0.0;
        $row['bat'] = $storageProjection['battery_w'] ?? null;
    }

    $runtimeMatchesSlot = is_array($canonicalSlot)
        && is_array($storageDispatchRuntime)
        && !empty($storageDispatchGeneration['runtime_coherent'])
        && (($storageDispatchRuntime['schema_version'] ?? '') === 'storage_dispatch_runtime_v1')
        && (($storageDispatchRuntime['plan_id'] ?? null) === ($storagePlanMeta['plan_id'] ?? null))
        && (($storageDispatchRuntime['slot_id'] ?? null) === ($canonicalSlot['slot_id'] ?? null));
    $runtimeCommandsAllowed = $runtimeMatchesSlot && !empty($storageDispatchRuntime['commands_allowed']);
    $runtimeSelected = $runtimeMatchesSlot && !empty($storageDispatchRuntime['selected']);
    $runtimeExecutable = $runtimeMatchesSlot && !empty($storageDispatchRuntime['executable']);
    $runtimeCandidate = $runtimeMatchesSlot && is_array($storageDispatchRuntime['candidate'] ?? null)
        ? $storageDispatchRuntime['candidate']
        : [];
    $runtimeCandidateAction = strtoupper((string)($runtimeCandidate['action'] ?? ''));
    $runtimeExportBudgetW = $runtimeMatchesSlot
        ? max(0.0, (float)($storageDispatchRuntime['export_budget_w'] ?? 0.0))
        : 0.0;
    $directMarketingState = forecastDirectMarketingSlotState(
        $storageProjection,
        $storageDispatchRuntime,
        $runtimeMatchesSlot,
        $forecastRequiresDirectMarketingPrice
    );
    $runtimeDirectMarketingCharge = $forecastRequiresDirectMarketingPrice
        && !empty($directMarketingState['active'])
        && ($directMarketingState['runtime_action'] ?? null) === 'PV_STORE'
        && ($directMarketingState['authorized_charge_w'] ?? 0.0) > 0.0;
    if ($runtimeCommandsAllowed && $runtimeSelected && $runtimeExecutable && $runtimeCandidateAction === 'HEADROOM_EXPORT') {
        $st_predump_executable_w = max(
            0.0,
            (float)($storageDispatchRuntime['export_budget_w'] ?? ($runtimeCandidate['power_w'] ?? 0.0))
        );
        $st_predump_status = 'selected_executable';
    }

    $target_soc = is_array($storageProjection)
        ? ($storageProjection['target_soc_pct'] ?? null)
        : null;
    $target_curve_source = is_array($canonicalSlot) ? 'canonical' : 'legacy_projection';
    $fallback_soc = $st_soc !== null ? (float)$st_soc : ($row['soc'] ?? null);
    $plan_soc = $st_soc !== null ? (float)$st_soc : null;
    $row['_forecast_predump_active'] = $st_predump_executable_w > 0.0;
    $row['_forecast_predump_w'] = $row['_forecast_predump_active'] ? $st_predump_executable_w : 0.0;
    $row['_forecast_predump_candidate_w'] = $st_predump_candidate_w;
    $row['_forecast_predump_status'] = $st_predump_status;

    // Reine Projektion: Batterie, SoC und Grid stammen aus demselben Slot.
    // PHP wählt keine Lade-/Entladerichtung und integriert keinen zweiten SoC.
    $st_soc_val = is_array($storageProjection)
        ? ($storageProjection['soc_pct'] ?? $fallback_soc)
        : $fallback_soc;
    $grid = is_array($storageProjection) && array_key_exists('grid_w', $storageProjection)
        ? $storageProjection['grid_w']
        : null;
    if ($grid === null && $row['pv'] !== null && $row['home'] !== null && $row['bat'] !== null) {
        $grid = (float)$row['home']
            + (float)($row['wp'] ?? 0.0)
            + (float)($row['climate'] ?? 0.0)
            + (float)($row['wb'] ?? 0.0)
            + (float)($row['wb2'] ?? 0.0)
            + (float)$row['bat']
            - (float)$row['pv'];
    }

    $data['pv'][] = $row['pv'] !== null ? round($row['pv']) : null;
    $data['home'][] = $row['home'] !== null ? round($row['home']) : null;
    $data['wp'][] = $row['wp'] !== null ? round($row['wp']) : null;
    $data['climate'][] = $row['climate'] !== null ? round($row['climate']) : null;
    $data['home_source'][] = is_array($storageProjection) ? ($storageProjection['home_source'] ?? null) : null;
    $data['home_quality'][] = is_array($storageProjection) ? ($storageProjection['home_quality'] ?? null) : null;
    $data['wp_source'][] = is_array($storageProjection) ? ($storageProjection['wp_source'] ?? null) : null;
    $data['wp_quality'][] = is_array($storageProjection) ? ($storageProjection['wp_quality'] ?? null) : null;
    $data['climate_source'][] = is_array($storageProjection) ? ($storageProjection['climate_source'] ?? null) : null;
    $data['climate_quality'][] = is_array($storageProjection) ? ($storageProjection['climate_quality'] ?? null) : null;
    $data['wb'][] = $row['wb'] !== null ? round($row['wb']) : null;
    $data['wb2'][] = $row['wb2'] !== null ? round($row['wb2']) : null;
    $data['soc'][] = $st_soc_val !== null ? round((float)$st_soc_val, 1) : null;
    // Zukunftsplanung stammt ausschließlich aus der plan_id/slot_id-gebundenen
    // Projektion. Nur der aktuelle Runtime-Slot darf Ausführbarkeit ergänzen.
    $directMarketingCandidate = $directMarketingState['candidate'];
    $directMarketingCandidateW = $directMarketingState['candidate_w'];
    $directMarketingSelected = $directMarketingState['selected'];
    $directMarketingExecutable = $directMarketingState['executable'];
    $directMarketingCommandsAllowed = $directMarketingState['commands_allowed'];
    $directMarketingPlanExecutable = $directMarketingState['plan_executable'];
    $directMarketingPlanCommandsAllowed = $directMarketingState['plan_commands_allowed'];
    $directMarketingBlockReason = $directMarketingState['block_reason'];
    $directMarketingExportW = $directMarketingState['authorized_export_w'];
    $directMarketingPlannedW = $directMarketingState['planned_w'];
    $directMarketingAuthorizedExportW = $directMarketingState['authorized_export_w'];
    $directMarketingHardwareEffect = $directMarketingState['hardware_effect'];
    $directMarketingChargeW = forecastDirectMarketingPlannedChargeW(
        $directMarketingState
    );
    // Der Adapter materialisiert die DV-Folgeprojektion einmal plan-id-gebunden.
    // PHP übernimmt sie nur; es integriert keine Energie und berechnet keinen SoC.
    $projPlanId = $data['direct_marketing_soc_projection']['plan_id'] ?? $data['storage_plan_id'] ?? null;
    $directMarketingSoc = is_array($storageProjection)
        && !empty($data['direct_marketing_soc_projection']['complete'])
        && ($projPlanId === ($data['storage_plan_id'] ?? null))
        && isset($storageProjection['direct_marketing_soc_pct'])
        && is_numeric($storageProjection['direct_marketing_soc_pct'])
        ? (float)$storageProjection['direct_marketing_soc_pct']
        : null;
    $directMarketingAction = $directMarketingState['active']
        ? ($directMarketingState['runtime_action'] ?? null)
        : ($directMarketingPlannedW > 0.0
            ? ($directMarketingState['plan_action'] ?? null)
            : ($runtimeDirectMarketingCharge ? 'PV_STORE' : null));
    $directMarketingCandidateAction = $directMarketingState['candidate_action'];
    $data['direct_marketing_export'][] = round($directMarketingExportW);
    $data['direct_marketing_charge'][] = round($directMarketingChargeW);
    $data['direct_marketing_soc'][] = $directMarketingSoc !== null ? round($directMarketingSoc, 1) : null;
    $data['direct_marketing_action'][] = $directMarketingAction;
    $data['direct_marketing_candidate'][] = $directMarketingCandidate;
    $data['direct_marketing_candidate_w'][] = round($directMarketingCandidateW);
    $data['direct_marketing_candidate_action'][] = $directMarketingCandidateAction;
    $data['direct_marketing_selected'][] = $directMarketingSelected;
    $data['direct_marketing_executable'][] = $directMarketingExecutable;
    $data['direct_marketing_commands_allowed'][] = $directMarketingCommandsAllowed;
    $data['direct_marketing_plan_executable'][] = $directMarketingPlanExecutable;
    $data['direct_marketing_plan_commands_allowed'][] = $directMarketingPlanCommandsAllowed;
    $data['direct_marketing_block_reason'][] = $directMarketingBlockReason;
    $data['direct_marketing_planned_w'][] = round($directMarketingPlannedW);
    $data['direct_marketing_authorized_export_w'][] = round($directMarketingAuthorizedExportW);
    $data['direct_marketing_active'][] = $directMarketingState['active'];
    $data['direct_marketing_hardware_effect'][] = $directMarketingHardwareEffect;
    $data['direct_marketing_selection_invariant'][] = [
        'valid' => $directMarketingState['selection_invariant_valid'],
        'reason_code' => $directMarketingState['selection_invariant_reason'],
    ];
    $data['direct_marketing_window_id'][] = $directMarketingState['window_id'];
    $data['direct_marketing_export_segment_id'][] = $directMarketingState['export_segment_id'];
    $data['direct_marketing_market_eligible'][] = is_array($storageProjection)
        && !empty($storageProjection['direct_marketing_market_eligible']);
    $data['direct_marketing_market_window_id'][] = is_array($storageProjection)
        ? ($storageProjection['direct_marketing_market_window_id'] ?? null)
        : null;
    $data['direct_marketing_market_window_start_ts'][] = is_array($storageProjection)
        ? ($storageProjection['direct_marketing_market_window_start_ts_ms'] ?? null)
        : null;
    $data['direct_marketing_market_window_end_ts'][] = is_array($storageProjection)
        ? ($storageProjection['direct_marketing_market_window_end_ts_ms'] ?? null)
        : null;
    $data['direct_marketing_market_margin_class'][] = is_array($storageProjection)
        ? ($storageProjection['direct_marketing_market_margin_class'] ?? null)
        : null;
    $data['direct_marketing_market_net_sell_ct'][] = is_array($storageProjection)
        ? ($storageProjection['direct_marketing_market_net_sell_ct'] ?? null)
        : null;
    $storageCurveValue = $target_soc !== null ? round($target_soc, 1) : null;
    $storageCurveDay = date('Y-m-d', (int)floor($targetTsMs / 1000));
    $storageCurveBreak = $storageCurveValue !== null
        && $lastStorageCurveValue !== null
        && (
            ($lastStorageCurveDay !== null && $storageCurveDay !== $lastStorageCurveDay)
            || ($lastStorageCurveSource !== null && $target_curve_source !== $lastStorageCurveSource)
        );
    $data['storage_target_curve'][] = $storageCurveBreak ? null : $storageCurveValue;
    if ($storageCurveValue !== null) {
        $lastStorageCurveDay = $storageCurveDay;
        $lastStorageCurveSource = $target_curve_source;
        $lastStorageCurveValue = $storageCurveValue;
    } else {
        $lastStorageCurveValue = null;
    }
    $data['market_charge'][] = is_array($storageProjection)
        ? round(max(0.0, (float)($storageProjection['market_charge_w'] ?? 0.0)))
        : 0;
    $data['market_hold'][] = is_array($storageProjection) && !empty($storageProjection['market_hold']) ? 1 : 0;
    $data['market_action'][] = is_array($storageProjection)
        ? ($storageProjection['market_action'] ?? null)
        : null;

    $gridIsNull = ($row['pv'] === null && $row['home'] === null && $row['bat'] === null);
    $data['grid'][] = $gridIsNull ? null : round($grid);
    $data['bat'][] = $row['bat'] !== null ? round($row['bat']) : null;
    $data['predump'][] = !empty($row['_forecast_predump_active']) ? 1 : 0;
    $data['predump_w'][] = round((float)($row['_forecast_predump_w'] ?? 0.0));
    $data['predump_candidate_w'][] = round((float)($row['_forecast_predump_candidate_w'] ?? 0.0));
    $data['predump_executable_w'][] = round((float)($row['_forecast_predump_w'] ?? 0.0));
    $data['predump_status'][] = $row['_forecast_predump_status'] ?? 'none';
    $dv_power = 0;
    if (isset($row['dv_grid_kwh']) && $row['dv_grid_kwh'] > 0) {
        $prev = $last_dv_kwh !== null ? $last_dv_kwh : 0;
        if ($row['dv_grid_kwh'] >= $prev) {
            $dv_power = ($row['dv_grid_kwh'] - $prev) * 4000; // kWh in 15 Min -> Watt
        }
        $last_dv_kwh = $row['dv_grid_kwh'];
    } else {
        $last_dv_kwh = null;
    }
    $data['dv_grid'][] = round($dv_power);

}
unset($row);

// Kurze 0-ct-Ausreisser am Tageswechsel entstehen bei 15-Minuten-Tarifen,
// wenn genau ein Slot im Join fehlt. Diese Slots sind kein reales Gratisfenster
// und duerfen weder Diagramm noch Preislogik nach unten reissen.
repairShortMidnightPriceGaps($data['price'], $data['labels']);

// --- Stabile Tagesprognose (Heute) ---
// V4 Override: Legacy C++ file parsing is disabled. `dailySums` calculation natively relies on V4 python output arrays.
$stableSums = null;

// --- Tagessummen für die Prognose-Anzeige (Morgen, Übermorgen) ---
$offsetHours = (int)date('Z') / 3600;
$dailySums = []; // key = 'today'/'tomorrow'/'day_after'

foreach ($merged as $row) {
    $tRaw = $row['tRaw'];

    // Tag bestimmen (0 = heute, 1 = morgen, 2 = uebermorgen)
    // V4-Slots haben tRaw > 10000 (Unix-Timestamp)
    if ($tRaw > 10000) {
        // V4 Unix-Timestamp: Tag relativ zu heutigem Mitternacht CEST berechnen
        $dayIndex = (int)floor(($tRaw - strtotime('today')) / 86400);
    } else {
        // Dezimal-Stunden (GMT) -> lokale Stunden (Legacy-Pfad, tritt mit leerem $lines nicht mehr auf)
        $tLocal = $tRaw + $offsetHours;
        $dayIndex = (int)floor($tLocal / 24);
    }
    if ($dayIndex < 0 || $dayIndex > 2) continue;

    $key = ['today', 'tomorrow', 'day_after'][$dayIndex];

    if (!isset($dailySums[$key])) {
        $dailySums[$key] = ['pv_kwh' => 0.0, 'home_kwh' => 0.0, 'wp_kwh' => 0.0, 'climate_kwh' => 0.0];
    }

    // Watt in 15-Min-Slots × 0,25h = kWh
    $dailySums[$key]['pv_kwh']   += $row['pv']   / 1000.0 * 0.25;
    $dailySums[$key]['home_kwh'] += $row['home']  / 1000.0 * 0.25;
    $dailySums[$key]['wp_kwh']   += $row['wp']    / 1000.0 * 0.25;
    $dailySums[$key]['climate_kwh'] += ($row['climate'] ?? 0.0) / 1000.0 * 0.25;
}

// Runden
foreach ($dailySums as $k => $v) {
    $dailySums[$k]['pv_kwh']   = round($v['pv_kwh'],   1);
    $dailySums[$k]['home_kwh'] = round($v['home_kwh'],  1);
    $dailySums[$k]['wp_kwh']   = round($v['wp_kwh'],    1);
    $dailySums[$k]['climate_kwh'] = round($v['climate_kwh'] ?? 0.0, 1);
}

// Überschreibe 'today' mit den KI/V4 Werten, falls sie vorhanden sind (komplette V4 Ablösung im UI)
$midnightMs = strtotime('today') * 1000;
$midnightNextMs = $midnightMs + (24 * 3600 * 1000);
$nowMs = (int)round(microtime(true) * 1000);
$todayRestStartMs = max($midnightMs, $nowMs);

$directMarketingActive = !empty($conf['direct_marketing_enabled'])
    || !empty($liveData['direct_marketing_enabled'])
    || (isset($storagePlanParsed['plan_meta']['direct_marketing_enabled']) && $storagePlanParsed['plan_meta']['direct_marketing_enabled'] === true);

if (!function_exists('sumStoragePlanWindowKwh')) {
    function sumStoragePlanWindowKwh($timeline, $startMs, $endMs, $dmActive = false) {
        if (!is_array($timeline) || empty($timeline)) return null;
        $sums = ['pv_kwh' => 0.0, 'home_kwh' => 0.0, 'wp_kwh' => 0.0, 'climate_kwh' => 0.0];
        $found = false;
        foreach ($timeline as $slot) {
            if (!isset($slot['ts'])) continue;
            $slotStart = (float)$slot['ts'];
            $slotEnd = $slotStart + 900000.0;
            if ($slotEnd <= $startMs || $slotStart >= $endMs) continue;
            $overlapMs = min($slotEnd, $endMs) - max($slotStart, $startMs);
            $weight = max(0.0, min(1.0, $overlapMs / 900000.0));
            if ($weight <= 0.0) continue;

            $slotPvW = max(0.0, (float)($slot['pv_w'] ?? 0.0));
            $slotHomeW = max(0.0, (float)($slot['home_w'] ?? 0.0));
            $slotWpW = max(0.0, (float)($slot['wp_w'] ?? 0.0));
            $slotClimateW = max(0.0, (float)($slot['climate_w'] ?? 0.0));
            $slotChargeW = max(0.0, (float)($slot['charge_w'] ?? ($slot['target_charge_w'] ?? 0.0)));

            // Bei Direktvermarktung und negativem Börsenpreis (<= 0):
            // Keine Netzeinspeisung / Zusatz-WR aus. Real nutzbarer Ertrag ist
            // nur Eigenverbrauch (Haus + WP + Klima) plus Batterieladung.
            $isNegPrice = false;
            if ($dmActive) {
                $dmPrice = isset($slot['direct_marketing_marketprice'])
                    ? (float)$slot['direct_marketing_marketprice']
                    : (isset($slot['direct_marketing_market_price_ct']) ? (float)$slot['direct_marketing_market_price_ct'] : null);
                if ($dmPrice !== null && $dmPrice <= 0.0) {
                    $isNegPrice = true;
                }
            }

            $effectivePvW = $isNegPrice
                ? min($slotPvW, $slotHomeW + $slotWpW + $slotClimateW + $slotChargeW)
                : $slotPvW;

            $sums['pv_kwh']   += $effectivePvW / 1000.0 * 0.25 * $weight;
            $sums['home_kwh'] += $slotHomeW / 1000.0 * 0.25 * $weight;
            $sums['wp_kwh']   += $slotWpW / 1000.0 * 0.25 * $weight;
            $sums['climate_kwh'] += $slotClimateW / 1000.0 * 0.25 * $weight;
            $found = true;
        }
        if (!$found) return null;
        foreach ($sums as $key => $value) $sums[$key] = round($value, 1);
        return $sums;
    }
}
if (!function_exists('sumStoragePlanRestOfTodayKwh')) {
    function sumStoragePlanRestOfTodayKwh($timeline, $startMs, $endMs, $dmActive = false) {
        return sumStoragePlanWindowKwh($timeline, $startMs, $endMs, $dmActive);
    }
}

// V4 KI-Haus, WP & Klima Werte (Rest für heute, bis Mitternacht) berechnen
if (isset($mlData) && $mlData) {
    if (!isset($dailySums['today'])) $dailySums['today'] = ['pv_kwh' => 0.0, 'home_kwh' => 0.0, 'wp_kwh' => 0.0, 'climate_kwh' => 0.0];

    $v4HomeSum = 0.0;
    $v4WpSum = 0.0;
    $v4ClimateSum = 0.0;
    if (isset($mlData['timeline']) && is_array($mlData['timeline'])) {
        foreach ($mlData['timeline'] as $slot) {
            if ($slot['start_timestamp'] >= $midnightMs && $slot['start_timestamp'] < $midnightNextMs) {
                // ml_predictor gibt "home_kwh" in kW (als Leistung) aus, also immer * 0.25 rechnen für Energie (kWh)
                $v4HomeSum += ((float)$slot['home_kwh'] * 0.25);
                $v4WpSum += ((float)$slot['wp_kwh'] * 0.25);
                $v4ClimateSum += ((float)($slot['climate_kwh'] ?? 0.0) * 0.25);
            }
        }
        $dailySums['today']['home_kwh'] = round($v4HomeSum, 1);
        $dailySums['today']['wp_kwh']   = round($v4WpSum, 1);
        $dailySums['today']['climate_kwh'] = round($v4ClimateSum, 1);
    } else {
        $dailySums['today']['home_kwh'] = round($mlData['home_kwh'], 1);
        $dailySums['today']['wp_kwh']   = round($mlData['wp_kwh'], 1);
        $dailySums['today']['climate_kwh'] = round((float)($mlData['climate_kwh'] ?? 0.0), 1);
    }
} elseif ($stableSums !== null) {
    if (!isset($dailySums['today'])) $dailySums['today'] = ['pv_kwh' => 0.0, 'home_kwh' => 0.0, 'wp_kwh' => 0.0, 'climate_kwh' => 0.0];
    $dailySums['today']['home_kwh'] = round($stableSums['home_kwh'], 1);
    $dailySums['today']['wp_kwh']   = round($stableSums['wp_kwh'], 1);
}

// V4 PV Ensemble Summation für die Kopfzeile.
$forecastPvDaySums = [];
if (isset($pvForecastsParsed) && $pvForecastsParsed) {
    $v4PvByDay = ['today' => 0.0, 'tomorrow' => 0.0, 'day_after' => 0.0];
    foreach ($pvForecastsParsed as $pf) {
        if (!isset($pf['start_timestamp'])) {
            continue;
        }
        $slotKw = forecastSlotPowerKw($pf);
        if ($slotKw === null) {
            continue;
        }
        $dayIndex = (int)floor((((float)$pf['start_timestamp']) - $midnightMs) / (24 * 3600 * 1000));
        if ($dayIndex < 0 || $dayIndex > 2) {
            continue;
        }
        $key = ['today', 'tomorrow', 'day_after'][$dayIndex];
        $v4PvByDay[$key] += $slotKw * 0.25;
    }
    foreach ($v4PvByDay as $key => $value) {
        if ($value <= 0.0) {
            continue;
        }
        if (!isset($dailySums[$key])) $dailySums[$key] = ['pv_kwh' => 0.0, 'home_kwh' => 0.0, 'wp_kwh' => 0.0, 'climate_kwh' => 0.0];
        $forecastPvDaySums[$key] = round($value, 1);
        $dailySums[$key]['pv_kwh'] = $forecastPvDaySums[$key];
    }
    if (!isset($dailySums['today'])) $dailySums['today'] = ['pv_kwh' => 0.0, 'home_kwh' => 0.0, 'wp_kwh' => 0.0, 'climate_kwh' => 0.0];
    $data['stable_today_pv_kwh'] = $forecastPvDaySums['today'] ?? round($dailySums['today']['pv_kwh'], 1);
} elseif ($stableSums !== null) {
    if (!isset($dailySums['today'])) $dailySums['today'] = ['pv_kwh' => 0.0, 'home_kwh' => 0.0, 'wp_kwh' => 0.0, 'climate_kwh' => 0.0];
    $dailySums['today']['pv_kwh'] = round($stableSums['pv_kwh'], 1);
    $data['stable_today_pv_kwh'] = round($stableSums['pv_kwh'], 1);
} else {
    $data['stable_today_pv_kwh'] = null;
}

// Für die Kopfzeile zählt "Heute" als Restprognose ab jetzt für Verbrauch, aber als voller Tagesertrag für PV.
$todayRest = (isset($storagePlanParsed['timeline']) && is_array($storagePlanParsed['timeline']))
    ? sumStoragePlanRestOfTodayKwh($storagePlanParsed['timeline'], $todayRestStartMs, $midnightNextMs, $directMarketingActive)
    : null;
$todayFullDay = (isset($storagePlanParsed['timeline']) && is_array($storagePlanParsed['timeline']))
    ? sumStoragePlanWindowKwh($storagePlanParsed['timeline'], $midnightMs, $midnightNextMs, $directMarketingActive)
    : null;

if ($todayRest !== null) {
    $todayPvKwh = ($todayFullDay !== null && ($todayFullDay['pv_kwh'] ?? 0.0) > 0.0)
        ? $todayFullDay['pv_kwh']
        : ($forecastPvDaySums['today'] ?? ($todayRest['pv_kwh'] ?? 0.0));
    $dailySums['today'] = [
        'pv_kwh' => round($todayPvKwh, 1),
        'home_kwh' => round($todayRest['home_kwh'], 1),
        'wp_kwh' => round($todayRest['wp_kwh'], 1),
        'climate_kwh' => round($todayRest['climate_kwh'], 1),
    ];
    $data['stable_today_pv_kwh'] = $dailySums['today']['pv_kwh'];
}

// Für Morgen/Übermorgen bereinigte Werte aus der Speicherplanung nutzen
if (isset($storagePlanParsed['timeline']) && is_array($storagePlanParsed['timeline'])) {
    $planDayWindows = [
        'tomorrow' => $midnightNextMs,
        'day_after' => $midnightNextMs + (24 * 3600 * 1000),
    ];
    foreach ($planDayWindows as $key => $dayStartMs) {
        $planDay = sumStoragePlanWindowKwh($storagePlanParsed['timeline'], $dayStartMs, $dayStartMs + (24 * 3600 * 1000), $directMarketingActive);
        if ($planDay === null) continue;
        if (!isset($dailySums[$key])) $dailySums[$key] = ['pv_kwh' => 0.0, 'home_kwh' => 0.0, 'wp_kwh' => 0.0, 'climate_kwh' => 0.0];
        $dailySums[$key]['home_kwh'] = round($planDay['home_kwh'], 1);
        $dailySums[$key]['wp_kwh'] = round($planDay['wp_kwh'], 1);
        $dailySums[$key]['climate_kwh'] = round($planDay['climate_kwh'], 1);
        $dailySums[$key]['pv_kwh'] = round(($planDay['pv_kwh'] > 0.0) ? $planDay['pv_kwh'] : ($forecastPvDaySums[$key] ?? 0.0), 1);
    }
}

$data['daily_summary'] = $dailySums;
$currentTopologyRevisions = array_values(array_unique(array_filter(
    $data['pv_topology_revision'] ?? [],
    static function ($value) {
        return is_string($value) && preg_match('/^sha256:[0-9a-f]{64}$/', $value);
    }
)));
$currentTopologyRevision = count($currentTopologyRevisions) === 1
    ? $currentTopologyRevisions[0]
    : null;
$data['pv_forecast_diagnostics'] = loadPvForecastDiagnosticEvidence(
    $currentTopologyRevision,
    pvForecastDiagnosticsEnabled($conf)
);
echo json_encode($data);
