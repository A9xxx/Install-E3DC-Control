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
// $now auf volle Stunde runden = saubere Slot-Grenzen fuer den Preis-Join mit eco_score.json.
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
    'direct_marketing_export' => [], 'direct_marketing_charge' => [], 'direct_marketing_soc' => [], 'direct_marketing_action' => [],
    'predump' => [], 'predump_w' => [],
    'pv_m1' => [], 'pv_m2' => [], 'pv_m3' => [], 'pv_ensemble' => [],
    'pv_history' => []   // KI-Prognose Vergangenheits-Overlay (was predicted, now past)

];

function forecastMarketContractForTs($marketPlan, $targetTsMs) {
    if (!is_array($marketPlan) || empty($marketPlan)) return null;
    $contracts = [];
    if (isset($marketPlan['active_contract']) && is_array($marketPlan['active_contract'])) {
        $contracts[] = $marketPlan['active_contract'];
    }
    if (isset($marketPlan['contracts']) && is_array($marketPlan['contracts'])) {
        foreach ($marketPlan['contracts'] as $contract) {
            if (is_array($contract)) $contracts[] = $contract;
        }
    }
    foreach ($contracts as $contract) {
        $start = isset($contract['start_ts']) ? (float)$contract['start_ts'] : 0.0;
        $end = isset($contract['end_ts']) ? (float)$contract['end_ts'] : 0.0;
        if ($start <= $targetTsMs && $targetTsMs < $end) {
            return $contract;
        }
    }
    return null;
}

function forecastMarketGridChargeAction($action) {
    return in_array((string)$action, ['grid_charge', 'grid_charge_candidate', 'negative_price_absorb'], true);
}

function forecastMarketContractReleases($contract, $consumer) {
    if (!is_array($contract) || !isset($contract['released_consumers']) || !is_array($contract['released_consumers'])) {
        return false;
    }
    $needle = strtolower((string)$consumer);
    foreach ($contract['released_consumers'] as $released) {
        if (strtolower(trim((string)$released)) === $needle) {
            return true;
        }
    }
    return false;
}

function forecastMarketHoldAction($action) {
    return in_array((string)$action, ['hold_discharge', 'house_supply_candidate'], true);
}

function forecastMarketPlannedChargeW($contract, $stBat, $maxChargeW) {
    $maxChargeW = max(0.0, (float)$maxChargeW);
    if ($maxChargeW <= 0.0 || !is_array($contract)) return 0.0;
    $action = (string)($contract['action'] ?? '');
    if ($action === 'negative_price_absorb') return $maxChargeW;
    if ($stBat !== null && (float)$stBat > 0.0) return min($maxChargeW, (float)$stBat);
    $forecast = isset($contract['forecast']) && is_array($contract['forecast']) ? $contract['forecast'] : [];
    $needWh = isset($forecast['grid_charge_need_wh']) ? max(0.0, (float)$forecast['grid_charge_need_wh']) : 0.0;
    $slots = max(1.0, (float)($contract['slot_count'] ?? 1.0));
    if ($needWh > 0.0) {
        return min($maxChargeW, max(300.0, ($needWh / $slots) / 0.25));
    }
    return min($maxChargeW, 1200.0);
}

function forecastDirectMarketingExportAction($action) {
    return in_array((string)$action, ['eco_plus_export_candidate', 'arbitrage_export_candidate'], true);
}

function forecastDirectMarketingWindows($directMarketingPlan) {
    if (!is_array($directMarketingPlan)) return [];
    return (isset($directMarketingPlan['windows']) && is_array($directMarketingPlan['windows']))
        ? $directMarketingPlan['windows']
        : [];
}

function forecastDirectMarketingWindowForTs($directMarketingPlan, $targetTsMs) {
    foreach (forecastDirectMarketingWindows($directMarketingPlan) as $window) {
        if (!is_array($window) || !forecastDirectMarketingExportAction($window['action'] ?? '')) continue;
        $start = isset($window['start_ts']) ? (float)$window['start_ts'] : 0.0;
        $end = isset($window['end_ts']) ? (float)$window['end_ts'] : 0.0;
        if ($start <= $targetTsMs && $targetTsMs < $end) {
            return $window;
        }
    }
    return null;
}

function forecastDirectMarketingHasExportWindows($directMarketingPlan) {
    foreach (forecastDirectMarketingWindows($directMarketingPlan) as $window) {
        if (is_array($window) && forecastDirectMarketingExportAction($window['action'] ?? '')) return true;
    }
    return false;
}

function forecastDirectMarketingExportPowerW($window) {
    if (!is_array($window) || !forecastDirectMarketingExportAction($window['action'] ?? '')) return 0.0;
    $power = isset($window['max_power_w']) ? max(0.0, (float)$window['max_power_w']) : 0.0;
    $start = isset($window['start_ts']) ? (float)$window['start_ts'] : 0.0;
    $end = isset($window['end_ts']) ? (float)$window['end_ts'] : 0.0;
    $durationH = $end > $start ? (($end - $start) / 3600000.0) : 0.0;
    $kwh = isset($window['theoretical_kwh']) ? max(0.0, (float)$window['theoretical_kwh']) : 0.0;
    if ($durationH > 0.0 && $kwh > 0.0) {
        $avgPower = ($kwh * 1000.0) / $durationH;
        $power = $power > 0.0 ? min($power, $avgPower) : $avgPower;
    }
    return $power;
}

function forecastDirectMarketingPolicyDecision($directMarketingPlan) {
    if (!is_array($directMarketingPlan)) return [];
    $policy = $directMarketingPlan['policy_decision'] ?? [];
    return is_array($policy) ? $policy : [];
}

function forecastDirectMarketingPolicyTimeline($directMarketingPlan) {
    if (!is_array($directMarketingPlan)) return [];
    $timeline = $directMarketingPlan['policy_timeline'] ?? [];
    return is_array($timeline) ? $timeline : [];
}

function forecastDirectMarketingPolicyDecisionForTs($directMarketingPlan, $targetTsMs) {
    foreach (forecastDirectMarketingPolicyTimeline($directMarketingPlan) as $policy) {
        if (!is_array($policy)) continue;
        $start = isset($policy['start_ts']) ? (float)$policy['start_ts'] : 0.0;
        $end = isset($policy['end_ts']) ? (float)$policy['end_ts'] : 0.0;
        if ($start <= $targetTsMs && $targetTsMs < $end) return $policy;
    }

    // Alte Pläne besitzen nur die Entscheidung für den aktuellen Slot. Diese
    // darf nicht mehr versehentlich auf alle späteren Fenster angewendet werden.
    $policy = forecastDirectMarketingPolicyDecision($directMarketingPlan);
    $selected = is_array($policy['selected_window'] ?? null) ? $policy['selected_window'] : [];
    $start = isset($selected['start_ts']) ? (float)$selected['start_ts'] : 0.0;
    $end = isset($selected['end_ts']) ? (float)$selected['end_ts'] : 0.0;
    return ($start <= $targetTsMs && $targetTsMs < $end) ? $policy : [];
}

function forecastDirectMarketingPolicyCommandsAllowed($policy) {
    if (!is_array($policy) || empty($policy)) return false;
    if (($policy['schema'] ?? null) !== 'direct_marketing_policy_v1') return false;
    if (!empty($policy['blocked'])) return false;
    if (array_key_exists('commands_allowed', $policy) && !$policy['commands_allowed']) return false;
    $target = strtoupper(trim((string)($policy['dv_target_state'] ?? '')));
    return in_array($target, ['FORCE_EXPORT', 'HEADROOM_EXPORT', 'FORCE_CHARGE_PV'], true);
}

function forecastDirectMarketingPolicyTargetState($policy) {
    return is_array($policy) ? strtoupper(trim((string)($policy['dv_target_state'] ?? ''))) : '';
}

function forecastDirectMarketingPolicyExportPowerW($policy, $windowPowerW) {
    if (!forecastDirectMarketingPolicyCommandsAllowed($policy)) return 0.0;
    $target = forecastDirectMarketingPolicyTargetState($policy);
    if (!in_array($target, ['FORCE_EXPORT', 'HEADROOM_EXPORT'], true)) return 0.0;
    $budget = 0.0;
    $storageBudget = $policy['storage_budget'] ?? [];
    if (is_array($storageBudget) && isset($storageBudget['export_budget_w'])) {
        $budget = max(0.0, (float)$storageBudget['export_budget_w']);
    }
    if ($budget <= 0.0) return 0.0;
    $windowPowerW = max(0.0, (float)$windowPowerW);
    return $windowPowerW > 0.0 ? min($windowPowerW, $budget) : $budget;
}

function forecastDirectMarketingPolicyChargePowerW($policy) {
    if (!forecastDirectMarketingPolicyCommandsAllowed($policy)) return 0.0;
    if (forecastDirectMarketingPolicyTargetState($policy) !== 'FORCE_CHARGE_PV') return 0.0;
    $storageBudget = $policy['storage_budget'] ?? [];
    return is_array($storageBudget) && isset($storageBudget['charge_budget_w'])
        ? max(0.0, (float)$storageBudget['charge_budget_w'])
        : 0.0;
}

function forecastDirectMarketingPolicyHasActiveSchedule($directMarketingPlan) {
    foreach (forecastDirectMarketingPolicyTimeline($directMarketingPlan) as $policy) {
        if (forecastDirectMarketingPolicyCommandsAllowed($policy)) return true;
    }
    return forecastDirectMarketingPolicyCommandsAllowed(
        forecastDirectMarketingPolicyDecision($directMarketingPlan)
    );
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
    // als 72h-SOC fuer morgen/uebermorgen fortschreiben.
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

function smoothDisplayRatio($ratio) {
    $ratio = max(0.0, min(1.0, (float)$ratio));
    return $ratio * $ratio * (3.0 - 2.0 * $ratio);
}

function interpolateDisplayStorageCurveSoc($timeline, $targetTsMs, $conf, $meta = []) {
    if (empty($timeline) || !is_array($timeline)) {
        return null;
    }

    $targetTs = (int)floor($targetTsMs / 1000);
    $dayStart = strtotime(date('Y-m-d', $targetTs) . ' 00:00:00');
    if ($dayStart === false) {
        return null;
    }
    $dayEnd = $dayStart + 86400;

    $slots = [];
    $lastPvTs = null;
    $lastSurplusTs = null;
    $firstSoc = null;

    foreach ($timeline as $point) {
        if (!isset($point['ts'])) {
            continue;
        }
        $ts = (int)floor(((int)$point['ts']) / 1000);
        if ($ts < $dayStart || $ts >= $dayEnd) {
            continue;
        }

        $pv = isset($point['pv_w']) ? (float)$point['pv_w'] : 0.0;
        $home = isset($point['home_w']) ? (float)$point['home_w'] : 0.0;
        $wp = isset($point['wp_w']) ? (float)$point['wp_w'] : 0.0;
        $surplus = isset($point['surplus_w']) ? (float)$point['surplus_w'] : ($pv - $home - $wp);
        $soc = isset($point['soc']) ? (float)$point['soc'] : null;
        if ($firstSoc === null && $soc !== null) {
            $firstSoc = $soc;
        }
        if ($pv > 100.0) {
            $lastPvTs = $ts;
        }
        if ($pv > 500.0 && $surplus > 200.0) {
            $lastSurplusTs = $ts;
        }

        $slots[] = ['ts' => $ts, 'pv' => $pv, 'soc' => $soc];
    }

    if (!$slots) {
        return null;
    }

    $morningSoc = isset($meta['morning_target'])
        ? (float)$meta['morning_target']
        : displayCurveConfigFloat($conf, 'storage_morning_soc', 20.0);
    if ($morningSoc <= 0.0) {
        $morningSoc = $firstSoc !== null ? $firstSoc : 0.0;
    }

    $targetSoc = displayCurveConfigFloat($conf, 'storage_target_soc', 90.0);

    $morningHour = displayCurveConfigFloat($conf, 'storage_morning_hour', 9.0);
    $startTs = $dayStart + (int)round($morningHour * 3600);

    $curveEndCandidate = null;
    if ($lastSurplusTs !== null) {
        $curveEndCandidate = $lastSurplusTs - (90 * 60);
    } elseif ($lastPvTs !== null) {
        $curveEndCandidate = $lastPvTs - (90 * 60);
    }
    $endTs = $curveEndCandidate !== null ? $curveEndCandidate : ($dayStart + (int)round(18.25 * 3600));
    $endTs = max($startTs + 3600, min($dayEnd - 900, $endTs));

    $anchors = [
        ['ts' => $startTs, 'soc' => clampDisplaySoc($morningSoc)]
    ];

    $configuredAnchors = [
        [
            'soc' => displayCurveConfigFloat($conf, 'storage_mid_target_soc', 0.0),
            'hour' => displayCurveConfigFloat($conf, 'storage_mid_hour', 11.0),
        ],
        [
            'soc' => displayCurveConfigFloat($conf, 'storage_noon_target_soc', 0.0),
            'hour' => displayCurveConfigFloat($conf, 'storage_noon_hour', 14.0),
        ],
    ];
    foreach ($configuredAnchors as $configuredAnchor) {
        $anchorSoc = (float)$configuredAnchor['soc'];
        $anchorTs = $dayStart + (int)round(((float)$configuredAnchor['hour']) * 3600);
        if ($anchorSoc > 0.0 && $anchorTs > $startTs && $anchorTs < $endTs) {
            $anchors[] = ['ts' => $anchorTs, 'soc' => clampDisplaySoc($anchorSoc)];
        }
    }

    $anchors[] = ['ts' => $endTs, 'soc' => clampDisplaySoc($targetSoc)];
    usort($anchors, function($a, $b) { return $a['ts'] <=> $b['ts']; });

    if ($targetTs <= $anchors[0]['ts']) {
        $nightStartSoc = clampDisplaySoc(max($targetSoc, $anchors[0]['soc']));
        if ($targetTs <= $dayStart) {
            return $nightStartSoc;
        }
        $span = max(1, $anchors[0]['ts'] - $dayStart);
        $ratio = smoothDisplayRatio(($targetTs - $dayStart) / $span);
        return clampDisplaySoc($nightStartSoc + (($anchors[0]['soc'] - $nightStartSoc) * $ratio));
    }
    $last = $anchors[count($anchors) - 1];
    if ($targetTs >= $last['ts']) {
        return $last['soc'];
    }

    for ($i = 1; $i < count($anchors); $i++) {
        $prev = $anchors[$i - 1];
        $next = $anchors[$i];
        if ($targetTs <= $next['ts']) {
            $span = max(1, $next['ts'] - $prev['ts']);
            $ratio = smoothDisplayRatio(($targetTs - $prev['ts']) / $span);
            return clampDisplaySoc($prev['soc'] + (($next['soc'] - $prev['soc']) * $ratio));
        }
    }

    return null;
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

function forecastMarketPriceCt($row, $fallback = null) {
    if (!is_array($row)) {
        return $fallback;
    }
    foreach (['market_price', 'marketprice'] as $key) {
        if (array_key_exists($key, $row) && is_numeric($row[$key])) {
            return round(((float)$row[$key]) / 10.0, 2);
        }
    }
    return $fallback;
}

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
$forecastSocState = null;
$forecastSocLastTsMs = null;
$forecastSocLastBatW = 0.0;
$forecastSocCapacityWh = max(1000.0, $batCapacity * 1000.0);
$directMarketingSocOffsetWh = 0.0;
$forecastPredumpEnabled = displayCurveConfigBool($conf, 'predump_enable', true);
$forecastMaxChargeW = displayCurveConfigFloat($conf, 'maximumladeleistung', 4500.0);
if ($forecastMaxChargeW > 0.0 && $forecastMaxChargeW < 100.0) {
    $forecastMaxChargeW *= 1000.0;
}
$forecastMaxChargeW = max(500.0, $forecastMaxChargeW);
$forecastMaxDischargeW = $forecastMaxChargeW;
$forecastPredumpFloorSoc = displayCurveConfigFloat(
    $conf,
    'storage_predump_min_soc',
    displayCurveConfigFloat(
        $conf,
        'eco_dump_min_soc',
        displayCurveConfigFloat($conf, 'ep_reserve_pct', 8.0)
    )
);
$forecastPredumpFloorSoc = max(0.0, min(100.0, $forecastPredumpFloorSoc));
$forecastDischargeFloorSoc = displayCurveEffectiveEpReservePct($conf);
$data['forecast_reserve_floor_soc'] = round($forecastDischargeFloorSoc, 1);
$forecastMorningSoc = max(0.0, min(100.0, displayCurveConfigFloat($conf, 'storage_morning_soc', 20.0)));
$forecastMorningHour = displayCurveConfigFloat($conf, 'storage_morning_hour', 9.0);
$forecastPredumpMaxW = min($forecastMaxDischargeW, 4500.0);
$forecastFeedLimitW = displayCurveConfigFloat($conf, 'einspeiselimit', 0.0);
if ($forecastFeedLimitW > 0.0 && $forecastFeedLimitW < 100.0) {
    $forecastFeedLimitW *= 1000.0;
}
$forecastAbregelBufferW = max(0.0, displayCurveConfigFloat($conf, 'abregel_puffer_w', 300.0));
$forecastExportLimitW = $forecastFeedLimitW > 0.0
    ? max(0.0, $forecastFeedLimitW - $forecastAbregelBufferW)
    : 0.0;
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
    $finalMarketPrice = forecastMarketPriceCt($row, null);
    $finalScore = null;
    $hasV4Price = false;
    $maxEcoTs = 0;

    if ($ecoScores && is_array($ecoScores)) {
        foreach ($ecoScores as $score) {
            if ($score['end_timestamp'] > $maxEcoTs) { $maxEcoTs = $score['end_timestamp']; }
            if ($targetTsMs >= $score['start_timestamp'] && $targetTsMs < $score['end_timestamp']) {
                $finalPrice = round($score['billing_price'], 2);
                $finalMarketPrice = forecastMarketPriceCt($score, null);
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
    // History-Buffer: Was hat das KI-Ensemble fuer vergangene Slots vorhergesagt?
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
    // Vergangenheits-Overlay: KI-Prognose fuer abgelaufene Slots
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
    static $storagePlanMaxTs  = 0;
    if ($storagePlanParsed === null) {
        $stFile = '/var/www/html/ramdisk/storage_plan.json';
        $stData = file_exists($stFile) ? @json_decode(file_get_contents($stFile), true) : false;
        $storagePlanParsed = $stData && isset($stData['timeline']) ? $stData['timeline'] : [];
        $storageTargetTimelineParsed = $stData && isset($stData['target_timeline']) ? $stData['target_timeline'] : [];
        $storagePlanMeta = is_array($stData) ? $stData : [];
        if ($storagePlanParsed) {
            $storagePlanMaxTs = $storagePlanParsed[count($storagePlanParsed)-1]['ts'];
        }
    }

    $st_bat = null; $st_soc = null; $st_home = null; $st_wp = null; $st_climate = null; $st_grid_dump = null;
    if (!empty($storagePlanParsed)) {
        foreach ($storagePlanParsed as $sP) {
            if ($targetTsMs >= $sP['ts'] && $targetTsMs < $sP['ts'] + 900000) {
                $st_bat  = $sP['charge_w'];
                $st_soc  = isset($sP['soc'])    ? $sP['soc']    : null;
                $st_home = isset($sP['home_w'])  ? $sP['home_w'] : null;
                $st_wp   = isset($sP['wp_w'])    ? $sP['wp_w']   : null;
                $st_climate = isset($sP['climate_w']) ? $sP['climate_w'] : null;
                $st_grid_dump = isset($sP['grid_dump_w']) ? $sP['grid_dump_w'] : null;
                // Sanfte End-Daempfung: In den letzten 6h der Simulations-Timeline
                // wird der Konfidenz-Score linear von 1.0 auf 0.7 reduziert
                // (verhindert harten Abschnitt, signalisiert "unsicherer Bereich")
                if ($storagePlanMaxTs > 0) {
                    $remainingMs = $storagePlanMaxTs - $targetTsMs;
                    $taperWindowMs = 6 * 3600 * 1000;
                    if ($remainingMs < $taperWindowMs && $remainingMs > 0) {
                        $taper = 0.7 + 0.3 * ($remainingMs / $taperWindowMs);
                        if ($st_home !== null) $st_home *= $taper;
                        if ($st_wp   !== null) $st_wp   *= $taper;
                        if ($st_climate !== null) $st_climate *= $taper;
                    }
                }
                break;
            }
        }
    }

    $marketContract = forecastMarketContractForTs($storagePlanMeta['market_plan'] ?? [], $targetTsMs);
    $marketAction = is_array($marketContract) ? (string)($marketContract['action'] ?? '') : '';
    $marketStorageReleased = forecastMarketContractReleases($marketContract, 'storage');
    $marketGridCharge = $marketStorageReleased && forecastMarketGridChargeAction($marketAction);
    $marketHold = $marketStorageReleased && forecastMarketHoldAction($marketAction);
    $marketPlannedChargeW = $marketGridCharge
        ? forecastMarketPlannedChargeW($marketContract, $st_bat, $forecastMaxChargeW)
        : 0.0;
    $directMarketingPlan = (isset($storagePlanMeta['direct_marketing']) && is_array($storagePlanMeta['direct_marketing']))
        ? $storagePlanMeta['direct_marketing']
        : [];
    $directMarketingPolicy = forecastDirectMarketingPolicyDecisionForTs($directMarketingPlan, $targetTsMs);
    $directMarketingPolicyTarget = forecastDirectMarketingPolicyTargetState($directMarketingPolicy);
    $directMarketingPolicyCanExport = forecastDirectMarketingPolicyCommandsAllowed($directMarketingPolicy)
        && in_array($directMarketingPolicyTarget, ['FORCE_EXPORT', 'HEADROOM_EXPORT'], true);
    $directMarketingWindow = forecastDirectMarketingWindowForTs($directMarketingPlan, $targetTsMs);
    $directMarketingWindowExportW = forecastDirectMarketingExportPowerW($directMarketingWindow);
    $directMarketingExportW = forecastDirectMarketingPolicyExportPowerW($directMarketingPolicy, $directMarketingWindowExportW);
    $directMarketingChargeBudgetW = forecastDirectMarketingPolicyChargePowerW($directMarketingPolicy);
    $directMarketingHasPolicyPlan = forecastDirectMarketingPolicyHasActiveSchedule($directMarketingPlan);

    // V4 KI-Arrays haben Vorrang vor Legacy-Werten (die durch das leere $lines-Array ohnehin leer sind).
    // Prioritaet: pv_ensemble > storage_plan > Leerwert
    if ($pv_ensemble !== null) $row['pv'] = $pv_ensemble;
    // Home, WP und Klima: ml_prediction hat Vorrang (genauere Vorhersage), storage_plan als Fallback für >50h
    if ($ml_home !== null) {
        $row['home'] = $ml_home;
    } elseif ($st_home !== null) {
        $row['home'] = $st_home;  // Fallback: storage_sim Wert fuer Stunden jenseits ml_prediction
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

    $target_soc = interpolateStorageTargetSoc($storageTargetTimelineParsed, $targetTsMs);
    $target_from_frozen_curve = ($target_soc !== null);
    $target_curve_source = $target_from_frozen_curve ? 'frozen' : 'display';
    if ($target_soc === null) {
        $target_soc = interpolateDisplayStorageCurveSoc($storagePlanParsed, $targetTsMs, $conf, $storagePlanMeta);
    }

    $nextTsMs = $targetTsMs + 900000;
    $target_soc_next = interpolateStorageTargetSoc($storageTargetTimelineParsed, $nextTsMs);
    $target_next_from_frozen_curve = ($target_soc_next !== null);
    if ($target_soc_next === null) {
        $target_soc_next = interpolateDisplayStorageCurveSoc($storagePlanParsed, $nextTsMs, $conf, $storagePlanMeta);
    }

    $fallback_soc = $target_soc !== null ? $target_soc : ($row['soc'] ?? null);
    $plan_soc = $st_soc !== null ? (float)$st_soc : null;

    if ($forecastSocState === null) {
        $initialSoc = $plan_soc !== null ? $plan_soc : ($fallback_soc !== null ? (float)$fallback_soc : null);
        $forecastSocState = $initialSoc !== null ? clampDisplaySoc(max($forecastDischargeFloorSoc, $initialSoc)) : null;
        $forecastSocLastTsMs = $targetTsMs;
    } elseif ($forecastSocState !== null && $forecastSocLastTsMs !== null) {
        $dtHours = max(0.0, min(1.0, ($targetTsMs - $forecastSocLastTsMs) / 3600000.0));
        $forecastSocState = clampDisplaySoc(max(
            $forecastDischargeFloorSoc,
            $forecastSocState + (($forecastSocLastBatW * $dtHours) / $forecastSocCapacityWh) * 100.0
        ));
        $forecastSocLastTsMs = $targetTsMs;
    }

    // Geregelt simulieren statt den rohen Batterieplan zu zeichnen:
    // PV deckt Haus/WP/Klima/Wallbox. Der Speicher folgt C++-nah der Ladekurve:
    // Pre-Dump schafft Platz, Abregelspitzen werden zusaetzlich in den Akku
    // geschoben, sonst bleibt Ueberschuss als Einspeisung sichtbar.
    if ($forecastSocState !== null && $row['pv'] !== null && $row['home'] !== null) {
        $slotHours = 0.25;
        $pvW = (float)($row['pv'] ?? 0.0);
        $homeW = (float)($row['home'] ?? 0.0);
        $wpW = (float)($row['wp'] ?? 0.0);
        $climateW = (float)($row['climate'] ?? 0.0);
        $wbW = (float)($row['wb'] ?? 0.0) + (float)($row['wb2'] ?? 0.0);
        $rawSurplusW = $pvW - $homeW - $wpW - $climateW - $wbW;
        $controlledBatW = 0.0;
        $predumpAvailablePct = $forecastPredumpEnabled ? max(0.0, $forecastSocState - $forecastPredumpFloorSoc) : 0.0;
        $predumpAvailableW = $forecastPredumpEnabled ? (($predumpAvailablePct / 100.0) * $forecastSocCapacityWh) / $slotHours : 0.0;
        $dischargeAvailablePct = max(0.0, $forecastSocState - $forecastDischargeFloorSoc);
        $dischargeAvailableW = (($dischargeAvailablePct / 100.0) * $forecastSocCapacityWh) / $slotHours;
        $row['_forecast_predump_active'] = false;
        $row['_forecast_predump_w'] = 0.0;

        $slotDayStartSec = strtotime(date('Y-m-d', (int)floor($targetTsMs / 1000)) . ' 00:00:00');
        $slotMorningTsMs = $slotDayStartSec !== false
            ? (($slotDayStartSec + (int)round($forecastMorningHour * 3600)) * 1000)
            : null;
        $planDumpW = $st_grid_dump !== null ? max(0.0, (float)$st_grid_dump) : 0.0;
        $predumpPlannedW = $planDumpW;

        // Pre-Dump im Chart ist ausschliesslich aktive Simulatorplanung.
        // Die Anzeige darf keine eigene Entladung zum Pre-Dump-Minimum erfinden.

        if ($forecastPredumpEnabled && $predumpPlannedW > 0.0 && $predumpAvailableW > 0.0) {
            $loadCoverW = $rawSurplusW < 0.0 ? abs($rawSurplusW) : 0.0;
            $predumpW = min(max($predumpPlannedW, $loadCoverW), $forecastPredumpMaxW, $predumpAvailableW);
            if ($forecastExportLimitW > 0.0) {
                $predumpW = min($predumpW, max(0.0, $forecastExportLimitW - $rawSurplusW));
            }
            if ($predumpW > 0.0) {
                $controlledBatW = -$predumpW;
                $row['_forecast_predump_active'] = true;
                $row['_forecast_predump_w'] = $predumpW;
            }
        }

        if (!$row['_forecast_predump_active'] && $marketGridCharge && $marketPlannedChargeW > 0.0) {
            $controlledBatW = min($forecastMaxChargeW, $marketPlannedChargeW);
        } elseif (!$row['_forecast_predump_active'] && $marketHold && $rawSurplusW < 0.0) {
            $controlledBatW = 0.0;
        } elseif (!$row['_forecast_predump_active'] && $rawSurplusW > 0.0) {
            $targetForSlot = $target_soc_next !== null ? $target_soc_next : ($target_soc !== null ? $target_soc : $forecastSocState);
            $needPct = max(0.0, $targetForSlot - $forecastSocState);
            $neededW = (($needPct / 100.0) * $forecastSocCapacityWh) / $slotHours;
            if ($target_soc !== null && $forecastSocState < $target_soc - 0.3) {
                $catchupPct = max(0.0, $target_soc - $forecastSocState);
                $neededW = max($neededW, (($catchupPct / 100.0) * $forecastSocCapacityWh) / $slotHours);
            }
            if ($target_soc === null && $target_soc_next === null && $st_bat !== null) {
                $neededW = max($neededW, (float)$st_bat);
            }
            $controlledBatW = min($rawSurplusW, $forecastMaxChargeW, max(0.0, $neededW));

            // C++-nahe Abregelreserve: wenn die Rest-Einspeisung ueber dem
            // konfigurierten Limit laege, wird die Batterie zusaetzlich geladen.
            $exportAfterChargeW = $rawSurplusW - $controlledBatW;
            if ($forecastExportLimitW > 0.0 && $exportAfterChargeW > $forecastExportLimitW && $forecastSocState < 99.8) {
                $socRoomW = (((100.0 - $forecastSocState) / 100.0) * $forecastSocCapacityWh) / $slotHours;
                $abregelExtraW = min(
                    $exportAfterChargeW - $forecastExportLimitW,
                    max(0.0, $forecastMaxChargeW - $controlledBatW),
                    max(0.0, $socRoomW)
                );
                $controlledBatW += max(0.0, $abregelExtraW);
            }
        } elseif (!$row['_forecast_predump_active'] && $rawSurplusW < 0.0) {
            $controlledBatW = -min(abs($rawSurplusW), $forecastMaxDischargeW, max(0.0, $dischargeAvailableW));
        }

        $row['bat'] = $controlledBatW;
    }


    $data['pv'][] = $row['pv'] !== null ? round($row['pv']) : null;
    $data['home'][] = $row['home'] !== null ? round($row['home']) : null;
    $data['wp'][] = $row['wp'] !== null ? round($row['wp']) : null;
    $data['climate'][] = $row['climate'] !== null ? round($row['climate']) : null;
    $data['wb'][] = $row['wb'] !== null ? round($row['wb']) : null;
    $data['wb2'][] = $row['wb2'] !== null ? round($row['wb2']) : null;

    // Fallback Variablen für die Mathe
    $c_home = $row['home'] ?? 0;
    $c_wp = $row['wp'] ?? 0;
    $c_climate = $row['climate'] ?? 0;
    $c_wb = ($row['wb'] ?? 0) + ($row['wb2'] ?? 0);
    $c_bat = $row['bat'] ?? 0;
    $c_pv = $row['pv'] ?? 0;

    // Bilanzgleichung: Grid = Home + WP + Klima + WB + BatterieLaden - PV
    $grid = $c_home + $c_wp + $c_climate + $c_wb + $c_bat - $c_pv;

    // Physikalischer Bilanzausgleich zwischen Batterie-, PV-, Haus- und Wärmepumpenwerten.
    $allowGridCharging = $marketGridCharge || ($finalPrice !== null && $finalPrice <= 15.0);

    if ($c_bat > 0 && $grid > 0 && !$allowGridCharging) {
        $correction = min($c_bat, $grid);
        $row['bat'] -= $correction;
        $c_bat -= $correction;
        $grid -= $correction;
    } elseif ($c_bat <= 0 && $grid > 0 && ($forecastSocState ?? ($row['soc'] ?? 0)) > ($forecastDischargeFloorSoc + 0.5)) {
        $correctionSoc = (float)($forecastSocState ?? ($row['soc'] ?? 0.0));
        $correctionDischargeW = (((max(0.0, $correctionSoc - $forecastDischargeFloorSoc) / 100.0) * $forecastSocCapacityWh) / 0.25);
        $maxDischargePossible = -min($forecastMaxDischargeW, $correctionDischargeW);
        $moeglicheErhoehung = $c_bat - $maxDischargePossible;
        if ($moeglicheErhoehung > 0) {
            $correction = min($moeglicheErhoehung, $grid);
            $row['bat'] -= $correction;
            $c_bat -= $correction;
            $grid -= $correction;
        }
    } elseif ($c_bat < 0 && $grid < 0 && empty($row['_forecast_predump_active'])) {
        $correction = min(abs($c_bat), abs($grid));
        $row['bat'] += $correction;
        $c_bat += $correction;
        $grid += $correction;
    }

    $st_soc_val = $forecastSocState !== null ? $forecastSocState : $fallback_soc;
    if ($st_soc_val !== null) {
        $st_soc_val = max($forecastDischargeFloorSoc, (float)$st_soc_val);
    }

    $data['soc'][] = $st_soc_val !== null ? round($st_soc_val, 1) : null;
    $directMarketingChargeW = 0.0;
    $directMarketingChargeDeltaW = 0.0;
    $directMarketingCommandsAllowed = forecastDirectMarketingPolicyCommandsAllowed($directMarketingPolicy);
    $directMarketingSuppressBaselineCharge = $directMarketingCommandsAllowed
        && in_array($directMarketingPolicyTarget, ['FORCE_EXPORT', 'HEADROOM_EXPORT'], true);
    $baselineChargeW = max(0.0, $c_bat);
    if ($directMarketingSuppressBaselineCharge) {
        $directMarketingChargeDeltaW = -$baselineChargeW;
    } elseif ($directMarketingChargeBudgetW > 0.0) {
        $policyPvSurplusW = max(0.0, $c_pv - $c_home - $c_wp - $c_climate - $c_wb);
        $directMarketingChargeW = min($directMarketingChargeBudgetW, $forecastMaxChargeW, $policyPvSurplusW);
        $directMarketingChargeDeltaW = $directMarketingChargeW - $baselineChargeW;
    }
    $directMarketingSoc = null;
    if ($directMarketingHasPolicyPlan && $st_soc_val !== null) {
        $directMarketingSoc = min(100.0, max(
            $forecastDischargeFloorSoc,
            (float)$st_soc_val + (($directMarketingSocOffsetWh / $forecastSocCapacityWh) * 100.0)
        ));
    }
    $data['direct_marketing_export'][] = round(max(0.0, $directMarketingExportW));
    $data['direct_marketing_charge'][] = round(max(0.0, $directMarketingChargeW));
    $data['direct_marketing_soc'][] = $directMarketingSoc !== null ? round($directMarketingSoc, 1) : null;
    $directMarketingSocOffsetWh += (
        $directMarketingChargeDeltaW - max(0.0, $directMarketingExportW)
    ) * 0.25;
    $data['direct_marketing_action'][] = $directMarketingCommandsAllowed
        ? ($directMarketingPolicy['source_action'] ?? ($directMarketingWindow['action'] ?? strtolower($directMarketingPolicyTarget)))
        : ($directMarketingPolicyTarget !== '' ? strtolower($directMarketingPolicyTarget) : null);
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
    $data['market_charge'][] = $marketGridCharge ? round($marketPlannedChargeW) : 0;
    $data['market_hold'][] = $marketHold ? 1 : 0;
    $data['market_action'][] = $marketAction !== '' ? $marketAction : null;

    $gridIsNull = ($row['pv'] === null && $row['home'] === null && $row['bat'] === null);
    $data['grid'][] = $gridIsNull ? null : round($grid);
    $data['bat'][] = $row['bat'] !== null ? round($row['bat']) : null;
    $data['predump'][] = !empty($row['_forecast_predump_active']) ? 1 : 0;
    $data['predump_w'][] = round((float)($row['_forecast_predump_w'] ?? 0.0));
    $forecastSocLastBatW = (float)($row['bat'] ?? 0.0);

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

// Fuer die Kopfzeile zaehlt "Heute" als Restprognose ab jetzt. Die normale
// Tagesaggregation oben bleibt fuer Morgen/Uebermorgen erhalten, aber heute
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
echo json_encode($data);
