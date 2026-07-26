<?php
require_once __DIR__ . '/helpers.php';
requireWebAuth(true);
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

function forecastReadDispatchGeneration($planFile, $runtimeFile, $maxAttempts = 2) {
    $attempts = max(1, (int)$maxAttempts);
    $lastPlan = [];
    $lastRuntime = [];
    $stablePlan = [];
    $canonicalInputSeen = false;
    $reason = 'snapshot_missing';
    for ($attempt = 1; $attempt <= $attempts; $attempt++) {
        $planBefore = file_exists($planFile)
            ? @json_decode(file_get_contents($planFile), true)
            : null;
        $runtime = file_exists($runtimeFile)
            ? @json_decode(file_get_contents($runtimeFile), true)
            : null;
        $planAfter = file_exists($planFile)
            ? @json_decode(file_get_contents($planFile), true)
            : null;
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

function loadPvForecastDiagnosticEvidence($currentTopologyRevision, $diagnosticsEnabled) {
    $metricLabels = [
        'trefferabweichung_wh' => 'Trefferabweichung',
        'richtungsversatz_wh' => 'Richtungsversatz',
        'energiegewichtete_gesamtabweichung_pct' => 'Energiegewichtete Gesamtabweichung',
        'vergleichsabdeckung_pct' => 'Vergleichsabdeckung',
    ];
    $fallback = [
        'schema_version' => 'pv_forecast_diagnostic_summary_v2',
        'status' => 'nicht_verfügbar',
        'available' => false,
        'provisional' => true,
        'operation_mode' => 'read_only_diagnostic',
        'control_effect' => false,
        'configuration_writes' => false,
        'automatic_model_selection' => false,
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
    if (
        !preg_match('/^sha256:[0-9a-f]{64}$/', $expectedRevision)
        || !file_exists($summaryPath)
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
        if (
            !is_array($payload)
            || ($payload['schema_version'] ?? '') !== 'pv_forecast_diagnostic_summary_v2'
            || ($payload['operation_mode'] ?? '') !== 'read_only_diagnostic'
            || ($payload['control_effect'] ?? null) !== false
            || ($payload['configuration_writes'] ?? null) !== false
            || ($payload['automatic_model_selection'] ?? null) !== false
            || ($payload['topology_revision'] ?? '') !== $expectedRevision
            || !is_array($payload['metrics'] ?? null)
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
        ] as $key) {
            $value = $payload[$key] ?? 0;
            if (!is_numeric($value) || (int)$value < 0) {
                return $fallback;
            }
            $nonnegativeIntegers[$key] = (int)$value;
        }
        $calculatedAt = $nonnegativeIntegers['calculated_at_utc_s'] ?? 0;
        if (
            $calculatedAt <= 0
            || $calculatedAt > (time() + 300)
            || (time() - $calculatedAt) > $maxSummaryAgeSeconds
        ) {
            return array_merge($fallback, ['reason' => 'zusammenfassung_veraltet']);
        }
        return array_merge($fallback, $nonnegativeIntegers, [
            'topology_revision' => $expectedRevision,
            'status' => substr((string)($payload['status'] ?? 'nicht_verfügbar'), 0, 32),
            'available' => ($payload['available'] ?? null) === true,
            'reason' => substr((string)($payload['reason'] ?? ''), 0, 80),
            'provisional' => ($payload['provisional'] ?? null) === true,
            'provisional_reasons' => array_slice(array_values(array_filter(
                $payload['provisional_reasons'] ?? [],
                'is_string'
            )), 0, 8),
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

    // NEU: V4 Storage Simulator (Phase 3: Sunset-Targeting & Peak-Shaving) einlesen
    // storage_plan.json ist die primäre Quelle für Batterie + SoC für das volle 72h Fenster!
    // Es enthält auch home_w, wp_w und climate_w (aus ml_prediction interpoliert) für die volle Horizon.
    static $storagePlanParsed = null;
    static $storageTargetTimelineParsed = null;
    static $storagePlanMeta = [];
    static $storageCanonicalSlotsParsed = [];
    static $storageDispatchRuntime = [];
    static $storageDispatchGeneration = [];
    if ($storagePlanParsed === null) {
        $stFile = '/var/www/html/ramdisk/storage_plan.json';
        $runtimeFile = '/var/www/html/ramdisk/storage_dispatch_runtime.json';
        $storageDispatchGeneration = forecastReadDispatchGeneration($stFile, $runtimeFile, 2);
        $stData = $storageDispatchGeneration['plan'] ?? [];
        $storagePlanParsed = $stData && isset($stData['timeline']) ? $stData['timeline'] : [];
        $storageTargetTimelineParsed = $stData && isset($stData['target_timeline']) ? $stData['target_timeline'] : [];
        $storagePlanMeta = is_array($stData) ? $stData : [];
        $storageCanonicalSlotsParsed = forecastCanonicalDispatchPlanValid($storagePlanMeta)
            ? ($storagePlanMeta['slots'] ?? [])
            : [];
        $runtimeData = $storageDispatchGeneration['runtime'] ?? [];
        $storageDispatchRuntime = is_array($runtimeData) ? $runtimeData : [];
    }

    $canonicalSlot = !empty($storageDispatchGeneration['plan_coherent'])
        ? forecastCanonicalDispatchSlotForTs($storagePlanMeta, $targetTsMs)
        : null;
    $storageProjection = forecastCanonicalDispatchProjection($canonicalSlot);
    $st_bat = null; $st_soc = null; $st_home = null; $st_wp = null; $st_climate = null;
    $st_predump_candidate_w = 0.0; $st_predump_executable_w = 0.0; $st_predump_status = 'none';
    $allowLegacyProjection = empty($storageDispatchGeneration['canonical_input_seen']);
    if (!is_array($storageProjection) && $allowLegacyProjection) {
        $legacySlot = null;
        if (!empty($storagePlanParsed)) {
            foreach ($storagePlanParsed as $sP) {
                if ($targetTsMs >= $sP['ts'] && $targetTsMs < $sP['ts'] + 900000) {
                    $legacySlot = $sP;
                    break;
                }
            }
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
    $data['storage_plan_id'] = $storagePlanMeta['plan_id'] ?? null;
    $data['storage_plan_generated_at'] = $storagePlanMeta['generated_at'] ?? null;
    $data['storage_plan_schema_version'] = $storagePlanMeta['schema_version'] ?? null;
    $data['storage_plan_valid_from'] = $storagePlanMeta['valid_from'] ?? null;
    $data['storage_plan_valid_until'] = $storagePlanMeta['valid_until'] ?? null;
    $data['storage_plan_horizon_end'] = $storagePlanMeta['horizon_end'] ?? null;
    $data['storage_plan_fresh'] = !empty($storageDispatchGeneration['plan_coherent'])
        && forecastCanonicalDispatchPlanValid($storagePlanMeta)
        && ((float)($storagePlanMeta['valid_from_ts_ms'] ?? 0) <= (microtime(true) * 1000.0))
        && ((microtime(true) * 1000.0) < (float)($storagePlanMeta['valid_until_ts_ms'] ?? 0));
    $data['storage_plan_meta'] = forecastCanonicalDispatchPlanValid($storagePlanMeta)
        ? [
            'schema_version' => $storagePlanMeta['schema_version'] ?? null,
            'plan_id' => $storagePlanMeta['plan_id'] ?? null,
            'generated_at' => $storagePlanMeta['generated_at'] ?? null,
            'valid_from' => $storagePlanMeta['valid_from'] ?? null,
            'valid_until' => $storagePlanMeta['valid_until'] ?? null,
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
                && !empty($storageDispatchGeneration['plan_coherent'])
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
        'complete' => !empty($directMarketingSocProjection['complete'])
            && !empty($storageDispatchGeneration['plan_coherent']),
        'plan_id' => $storagePlanMeta['plan_id'] ?? null,
        'action_horizon_end_ts_ms' => $directMarketingSocProjection['action_horizon_end_ts_ms'] ?? null,
        'projection_horizon_end_ts_ms' => $directMarketingSocProjection['projection_horizon_end_ts_ms'] ?? null,
        'soc_source' => $directMarketingSocProjection['soc_source'] ?? null,
        'reason_code' => !empty($directMarketingSocProjection['complete'])
            ? null
            : ($directMarketingSocProjection['status'] ?? 'DIRECT_MARKETING_SOC_PROJECTION_INCOMPLETE'),
    ];
    $data['storage_dispatch_runtime'] = $storageDispatchRuntime;
    $data['storage_dispatch_generation'] = forecastDispatchGenerationDiagnostics($storageDispatchGeneration);
    $data['storage_projection_status'] = array_merge(
        $data['storage_dispatch_generation'],
        [
            'plan_fresh' => !empty($data['storage_plan_fresh']),
            'soc_curve_current' => !empty($storageDispatchGeneration['plan_coherent'])
                && !empty($data['storage_plan_fresh']),
        ]
    );
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
    $data['pv_e3dc_dc'][] = isset($canonicalForecast['e3dc_dc_pv']['p50'])
        ? round((float)$canonicalForecast['e3dc_dc_pv']['p50'])
        : null;
    $data['pv_external_ac'][] = isset($canonicalForecast['external_ac_pv']['p50'])
        ? round((float)$canonicalForecast['external_ac_pv']['p50'])
        : null;
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
    $directMarketingChargeW = $runtimeDirectMarketingCharge
        ? (float)$directMarketingState['authorized_charge_w']
        : 0.0;
    // Der Adapter materialisiert die DV-Folgeprojektion einmal plan-id-gebunden.
    // PHP übernimmt sie nur; es integriert keine Energie und berechnet keinen SoC.
    $directMarketingSoc = is_array($storageProjection)
        && !empty($data['direct_marketing_soc_projection']['complete'])
        && (($data['direct_marketing_soc_projection']['plan_id'] ?? null) === ($data['storage_plan_id'] ?? null))
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

if (!function_exists('sumStoragePlanWindowKwh')) {
    function sumStoragePlanWindowKwh($timeline, $startMs, $endMs) {
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
            $sums['pv_kwh']   += max(0.0, (float)($slot['pv_w'] ?? 0.0)) / 1000.0 * 0.25 * $weight;
            $sums['home_kwh'] += max(0.0, (float)($slot['home_w'] ?? 0.0)) / 1000.0 * 0.25 * $weight;
            $sums['wp_kwh']   += max(0.0, (float)($slot['wp_w'] ?? 0.0)) / 1000.0 * 0.25 * $weight;
            $sums['climate_kwh'] += max(0.0, (float)($slot['climate_w'] ?? 0.0)) / 1000.0 * 0.25 * $weight;
            $found = true;
        }
        if (!$found) return null;
        foreach ($sums as $key => $value) $sums[$key] = round($value, 1);
        return $sums;
    }
}
if (!function_exists('sumStoragePlanRestOfTodayKwh')) {
    function sumStoragePlanRestOfTodayKwh($timeline, $startMs, $endMs) {
        return sumStoragePlanWindowKwh($timeline, $startMs, $endMs);
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
// predicted_kwh ist historisch falsch benannt und enthält die mittlere kW-Leistung.
// Falls das Ensemble fehlt, aber Einzelmodelle sichtbar sind, nutzen wir deren Mittelwert.
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

// Für die Kopfzeile zählt "Heute" als Restprognose ab jetzt. Die normale
// Tagesaggregation oben bleibt für Morgen/Übermorgen erhalten, aber heute
// darf spaet am Abend nicht mehr den bereits vergangenen Haus-/WP-Verbrauch
// anzeigen.
$todayRest = (isset($storagePlanParsed) && $storagePlanParsed)
    ? sumStoragePlanRestOfTodayKwh($storagePlanParsed, $todayRestStartMs, $midnightNextMs)
    : null;
if ($todayRest !== null) {
    if (($todayRest['pv_kwh'] ?? 0.0) <= 0.0 && isset($forecastPvDaySums['today']) && $forecastPvDaySums['today'] > 0.0) {
        $todayRest['pv_kwh'] = $forecastPvDaySums['today'];
    }
    $dailySums['today'] = $todayRest;
    $data['stable_today_pv_kwh'] = $todayRest['pv_kwh'];
}

// Für Morgen/Übermorgen muss die Kopfzeile dieselbe bereinigte
// Verbrauchsbasis zeigen wie die Speicherplanung. Die rohe ML-Prognose kann
// z.B. vor der Nachtverbrauchs-Sanity sichtbar höher liegen.
if (isset($storagePlanParsed) && $storagePlanParsed) {
    $planDayWindows = [
        'tomorrow' => $midnightNextMs,
        'day_after' => $midnightNextMs + (24 * 3600 * 1000),
    ];
    foreach ($planDayWindows as $key => $dayStartMs) {
        $planDay = sumStoragePlanWindowKwh($storagePlanParsed, $dayStartMs, $dayStartMs + (24 * 3600 * 1000));
        if ($planDay === null) continue;
        if (!isset($dailySums[$key])) $dailySums[$key] = ['pv_kwh' => 0.0, 'home_kwh' => 0.0, 'wp_kwh' => 0.0, 'climate_kwh' => 0.0];
        $dailySums[$key]['home_kwh'] = $planDay['home_kwh'];
        $dailySums[$key]['wp_kwh'] = $planDay['wp_kwh'];
        $dailySums[$key]['climate_kwh'] = $planDay['climate_kwh'];
        if (($dailySums[$key]['pv_kwh'] ?? 0.0) <= 0.0) {
            $dailySums[$key]['pv_kwh'] = $planDay['pv_kwh'];
        }
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
