<?php
// langzeit.php - Monats- und Jahres-Statistiken via SQLite
require_once __DIR__ . '/helpers.php';
if (!headers_sent()) {
    header('Content-Type: text/html; charset=utf-8');
}
$dbPath = '/var/www/html/data/e3dc_stats.db';
require_once __DIR__ . '/eeg_tariff_tables.php';

// --- AJAX Handler für Statistik-Editor ---
if (isset($_GET['ajax']) && $_GET['ajax'] == '1') {
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store, no-cache, must-revalidate, max-age=0');
    header('Pragma: no-cache');
    requireWebAuth(true);
    if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
        http_response_code(405);
        echo json_encode(['success' => false, 'error' => 'Method not allowed']);
        exit;
    }
    e3dcRequireCsrfToken(true);
    $action = $_POST['action'] ?? 'save';
    $date = $_POST['date'] ?? null;

    if (!$date) {
        http_response_code(400);
        echo json_encode(['success' => false, 'error' => 'Kein Datum angegeben.']);
        exit;
    }

    if ($action === 'delete') {
        $res = saveDailyStats($date, [], 'delete');
    } else {
        $res = saveDailyStats($date, $_POST, 'save');
    }

    if (!$res) http_response_code(500);
    echo json_encode([
        'success' => (bool)$res,
        'error' => $res ? null : 'Die Statistikänderung konnte nicht bestätigt gespeichert werden. Bitte Datenbankrechte und freien Speicher prüfen.',
    ]);
    exit;
}

$dailyData = [];
$monthlyData = [];
$yearlyData = [];
$dbError = null;

function lzHasRealCostData($row) {
    foreach (['cost_total', 'cost_home', 'cost_wb', 'cost_wb2', 'cost_wp', 'cost_climate'] as $key) {
        if (abs((float)($row[$key] ?? 0)) > 0.0001) return true;
    }
    return false;
}

function lzRefreshDerivedCostFlags(&$row) {
    $hasRealCost = lzHasRealCostData($row);
    $row['grid_in_real'] = $hasRealCost ? (float)($row['grid_in'] ?? 0) : 0;
    $row['home_real'] = $hasRealCost ? (float)($row['home'] ?? 0) : 0;
    $row['wb_real'] = $hasRealCost ? (float)($row['wb'] ?? 0) : 0;
    $row['wp_real'] = $hasRealCost ? (float)($row['wp'] ?? 0) : 0;
    $row['climate_real'] = $hasRealCost ? (float)($row['climate'] ?? 0) : 0;
    $row['bat_in_sim'] = $hasRealCost ? 0 : (float)($row['bat_in'] ?? 0);
}

function lzReadLiveSavedFields($liveStats = null) {
    $saved = is_array($liveStats ?? null) ? ($liveStats['saved'] ?? []) : [];
    $derating = (float)($saved['derating_today_kwh'] ?? $saved['saved_derating_today_kwh'] ?? $liveStats['saved_td'] ?? 0);
    $inverter = (float)($saved['inverter_today_kwh'] ?? $saved['saved_inverter_today_kwh'] ?? $liveStats['saved_wb'] ?? 0);
    $total = (float)($saved['total_today_kwh'] ?? $liveStats['saved_u'] ?? 0);

    if ($total <= 0.0001) {
        $liveFile = '/var/www/html/ramdisk/live_data_py.json';
        if (file_exists($liveFile)) {
            $liveData = @json_decode(file_get_contents($liveFile), true);
            if (is_array($liveData)) {
                $derating = (float)($liveData['saved_derating_today_kwh'] ?? $derating);
                $inverter = (float)($liveData['saved_inverter_today_kwh'] ?? $inverter);
                $total = $derating + $inverter;
            }
        }
    }

    return [
        'saved_u' => round(max(0, $total), 3),
        'saved_td' => round(max(0, $derating), 3),
        'saved_wb' => round(max(0, $inverter), 3),
    ];
}

function lzFloat($row, $key) {
    return max(0.0, (float)($row[$key] ?? 0));
}

function lzStatsSum($stats, $keys) {
    $sum = 0.0;
    if (!is_array($stats)) return $sum;
    foreach ($keys as $key) {
        $sum += max(0.0, (float)($stats[$key] ?? 0));
    }
    return $sum;
}

function lzBuildBalanceRestFromStats($stats, $pv, $gridIn, $gridOut, $batIn, $batOut, $home, $wb1, $wb2, $wp, $climate) {
    $pvRest = max(0.0, (float)$pv - lzStatsSum($stats, [
        'pv_home_kwh', 'pv_wb_kwh', 'pv_wb2_kwh', 'pv_wp_kwh',
        'pv_climate_kwh', 'pv_bat_kwh', 'pv_grid_kwh',
    ]));
    $batRest = max(0.0, (float)$batOut - lzStatsSum($stats, [
        'bat_home_kwh', 'bat_wb_kwh', 'bat_wb2_kwh', 'bat_wp_kwh', 'bat_climate_kwh', 'bat_grid_kwh',
    ]));
    $knownUse = (float)$home + (float)$wb1 + (float)$wb2 + (float)$wp + (float)$climate + (float)$gridOut + (float)$batIn;
    $totalRest = max(0.0, ((float)$pv + (float)$gridIn + (float)$batOut) - $knownUse);
    return [
        'pv_balance_rest' => round($pvRest, 3),
        'bat_balance_rest' => round($batRest, 3),
        'balance_unknown_rest' => round(max(0.0, $totalRest - $pvRest - $batRest), 3),
    ];
}

function lzAddPvTotalFields(&$row) {
    $pv = lzFloat($row, 'pv');
    $extPv = lzFloat($row, 'ext_pv');
    $gridOut = lzFloat($row, 'grid_out');
    $batOut = lzFloat($row, 'bat_out');
    $pvTotal = max(lzFloat($row, 'pv_total'), $pv + $extPv);
    $exportFallback = 0.0;

    if ($extPv <= 0.0001 && $pv > 0.0001 && $gridOut > 0.5 && $gridOut > ($pv + 0.5) && $gridOut > max(1.0, $batOut * 1.5)) {
        $exportFallback = $gridOut;
        $pvTotal = max($pvTotal, $pv + $exportFallback);
    }

    $row['pv_raw'] = round($pv, 3);
    $row['pv_export_fallback'] = round($exportFallback, 3);
    $row['pv_total'] = round(max($pv, $pvTotal), 3);
}

function lzAddPvTotalFieldsToRows(&$rows) {
    foreach ($rows as &$row) {
        lzAddPvTotalFields($row);
    }
    unset($row);
}

function lzReadV4Config() {
    $configFile = '/var/www/html/data/e3dc_v4.json';
    if (!file_exists($configFile)) return [];
    $data = @json_decode((string)@file_get_contents($configFile), true);
    return is_array($data) ? $data : [];
}

function lzConfigString($config, $key, $default = '') {
    if (!is_array($config) || !array_key_exists($key, $config)) return $default;
    $value = $config[$key];
    if (is_array($value) && array_key_exists('value', $value)) {
        $value = $value['value'];
    }
    $value = trim((string)$value);
    return $value !== '' ? $value : $default;
}

function lzConfigBool($config, $key, $default = false) {
    $value = strtolower(lzConfigString($config, $key, $default ? '1' : '0'));
    return in_array($value, ['1', 'true', 'yes', 'on', 'ein'], true);
}

function lzConfigFloat($config, $key, $default = 0.0) {
    $raw = str_replace(',', '.', lzConfigString($config, $key, ''));
    return is_numeric($raw) ? (float)$raw : (float)$default;
}

function lzParseDecimal($value) {
    $value = str_replace(["\xc2\xa0", ' '], '', trim((string)$value));
    $value = str_replace(',', '.', $value);
    return is_numeric($value) ? (float)$value : null;
}

function lzForecastCapacityKwp($config) {
    $total = 0.0;
    foreach (['forecast1', 'forecast2', 'forecast3'] as $key) {
        $raw = lzConfigString($config, $key, '');
        if ($raw === '') continue;
        $parts = preg_split('/[\/;|\s]+/', str_replace(',', '.', $raw));
        $lastNumber = null;
        foreach ($parts as $part) {
            $num = lzParseDecimal($part);
            if ($num !== null && $num > 0) {
                $lastNumber = $num;
            }
        }
        if ($lastNumber !== null) {
            $total += $lastNumber;
        }
    }
    return $total;
}

function lzParseEegTariffTiers($raw) {
    $tiers = [];
    $lines = preg_split('/\r\n|\r|\n/', (string)$raw);
    foreach ($lines as $line) {
        $line = preg_replace('/#.*/', '', trim($line));
        if ($line === '') continue;
        preg_match_all('/-?\d+(?:[,.]\d+)?/', $line, $matches);
        $numbers = array_map('lzParseDecimal', $matches[0] ?? []);
        $numbers = array_values(array_filter($numbers, function($n) { return $n !== null; }));
        if (count($numbers) >= 2) {
            $limit = (float)$numbers[0];
            $rate = (float)$numbers[1];
        } elseif (count($numbers) === 1 && empty($tiers)) {
            $limit = 0.0;
            $rate = (float)$numbers[0];
        } else {
            continue;
        }
        if ($rate > 0) {
            $tiers[] = ['limit_kwp' => max(0.0, $limit), 'rate_ct' => $rate];
        }
    }
    usort($tiers, function($a, $b) {
        return ($a['limit_kwp'] <=> $b['limit_kwp']);
    });
    return $tiers;
}

function lzEegCurrentTable() {
    return e3dc_eeg_tariff_archive_meta();
}

function lzEegCurrentTiersFromConfig($config) {
    return e3dc_eeg_tariff_tiers_for_config($config);
}

function lzEegCurrentTableWarning($commissioning, $rateSource = 'bnetza_archive') {
    return e3dc_eeg_tariff_source_warning($commissioning, $rateSource);
}

function lzWeightedEegTariffCt($tiers, $capacityKwp) {
    if (empty($tiers)) return 0.0;
    if (count($tiers) === 1 && (float)$tiers[0]['limit_kwp'] <= 0.0) {
        return (float)$tiers[0]['rate_ct'];
    }
    $maxTier = 0.0;
    foreach ($tiers as $tier) {
        $maxTier = max($maxTier, (float)$tier['limit_kwp']);
    }
    $capacity = $capacityKwp > 0.0 ? $capacityKwp : $maxTier;
    if ($capacity <= 0.0) return 0.0;
    $prev = 0.0;
    $weighted = 0.0;
    foreach ($tiers as $tier) {
        $limit = (float)$tier['limit_kwp'];
        $rate = (float)$tier['rate_ct'];
        $segment = max(0.0, min($capacity, $limit) - $prev);
        if ($segment > 0.0) {
            $weighted += $segment * $rate;
            $prev += $segment;
        }
        if ($prev >= $capacity) break;
    }
    if ($prev < $capacity && !empty($tiers)) {
        $last = $tiers[count($tiers) - 1];
        $weighted += ($capacity - $prev) * (float)$last['rate_ct'];
    }
    return $weighted / $capacity;
}

function lzBuildEegConfig($config) {
    $enabled = lzConfigBool($config, 'direct_marketing_eeg_enable', false);
    $rateSource = lzConfigString($config, 'direct_marketing_eeg_rate_source', 'manual');
    $autoRateSource = in_array($rateSource, ['bnetza_archive', 'bnetza_current_2026_02'], true);
    $tiers = $autoRateSource
        ? lzEegCurrentTiersFromConfig($config)
        : lzParseEegTariffTiers(lzConfigString($config, 'direct_marketing_eeg_tariff_tiers', ''));
    $capacityKwp = lzForecastCapacityKwp($config);
    $tariffCt = lzWeightedEegTariffCt($tiers, $capacityKwp);
    $commissioning = lzConfigString($config, 'direct_marketing_eeg_commissioning_date', '');
    $supportYears = max(0, (int)round(lzConfigFloat($config, 'direct_marketing_eeg_support_years', 20)));
    $table = lzEegCurrentTable();
    $supportUntil = '';
    $inSupport = true;

    if ($commissioning !== '' && preg_match('/^\d{4}-\d{2}-\d{2}$/', $commissioning)) {
        $year = (int)substr($commissioning, 0, 4);
        $supportUntil = sprintf('%04d-12-31', $year + $supportYears);
        $today = new DateTimeImmutable('today');
        $end = DateTimeImmutable::createFromFormat('Y-m-d', $supportUntil) ?: null;
        $inSupport = $end ? ($today <= $end) : true;
    }

    return [
        'enabled' => $enabled,
        'capacity_kwp' => round($capacityKwp, 3),
        'tariff_ct' => round($tariffCt, 4),
        'tiers' => $tiers,
        'rate_source' => $rateSource,
        'system_type' => lzConfigString($config, 'direct_marketing_eeg_system_type', 'building'),
        'feed_type' => lzConfigString($config, 'direct_marketing_eeg_feed_type', 'partial'),
        'compensation_basis' => lzConfigString($config, 'direct_marketing_eeg_compensation_basis', 'feed_in_tariff'),
        'table_source' => $autoRateSource ? $table['source'] : '',
        'table_valid_from' => $autoRateSource ? $table['valid_from'] : '',
        'table_valid_to' => $autoRateSource ? $table['valid_to'] : '',
        'source_warning' => $autoRateSource ? lzEegCurrentTableWarning($commissioning, $rateSource) : '',
        'commissioning_date' => $commissioning,
        'support_years' => $supportYears,
        'support_until' => $supportUntil,
        'in_support' => $enabled && $tariffCt > 0.0 && $inSupport,
    ];
}

if (file_exists($dbPath)) {
    try {
        $db = new PDO('sqlite:' . $dbPath);
        $db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);

        // --- Auto-Migration: Neue Kosten-Spalten anlegen falls sie fehlen ---
        $newCols = ['cost_total', 'cost_home', 'cost_bat', 'cost_wb', 'cost_wp', 'wb2_consumption', 'cost_wb2', 'climate_consumption', 'cost_climate', 'pv_balance_rest', 'bat_balance_rest', 'balance_unknown_rest', 'saved_u', 'saved_td', 'saved_wb', 'pv_e3dc', 'pv_external', 'pv_source_rest', 'pv_grid', 'bat_grid'];
        foreach ($newCols as $col) {
            try { $db->exec("ALTER TABLE daily_stats ADD COLUMN $col REAL DEFAULT 0"); } catch (Exception $e) { }
        }

// --- Auto-Migration: Index für massive Beschleunigung der Langzeit-Statistiken ---
        try {
            $db->exec("CREATE INDEX IF NOT EXISTS idx_ml_date ON ml_training_data(date)");
            $db->exec("CREATE INDEX IF NOT EXISTS idx_ds_date ON daily_stats(date DESC)");
        } catch (Exception $e) { }

        $cutoffDate = date('Y-m-d', strtotime('-35 days'));
        $realCostDayExpr = "(ABS(COALESCE(d.cost_total, 0)) > 0.0001 OR ABS(COALESCE(d.cost_home, 0)) > 0.0001 OR ABS(COALESCE(d.cost_wb, 0)) > 0.0001 OR ABS(COALESCE(d.cost_wb2, 0)) > 0.0001 OR ABS(COALESCE(d.cost_wp, 0)) > 0.0001 OR ABS(COALESCE(d.cost_climate, 0)) > 0.0001)";
        $realCostAggExpr = "(ABS(COALESCE(cost_total, 0)) > 0.0001 OR ABS(COALESCE(cost_home, 0)) > 0.0001 OR ABS(COALESCE(cost_wb, 0)) > 0.0001 OR ABS(COALESCE(cost_wb2, 0)) > 0.0001 OR ABS(COALESCE(cost_wp, 0)) > 0.0001 OR ABS(COALESCE(cost_climate, 0)) > 0.0001)";

        // Hole Tagesdaten (letzte 30 Tage) inkl. ML-Trainingsdaten
        $queryDay = "SELECT
            d.date as day,
            d.pv_yield as pv,
            d.home_consumption as home,
            d.grid_in as grid_in,
            d.grid_out as grid_out,
            d.bat_in as bat_in,
            d.bat_out as bat_out,
            COALESCE(d.wb_consumption, 0) as wb1,
            COALESCE(d.wb2_consumption, 0) as wb2,
            (COALESCE(d.wb_consumption, 0) + COALESCE(d.wb2_consumption, 0)) as wb,
            d.wp_consumption as wp,
            COALESCE(d.climate_consumption, 0) as climate,
            COALESCE(d.cost_total, 0) as cost_total,
            COALESCE(d.cost_home, 0) as cost_home,
            (COALESCE(d.cost_wb, 0) + COALESCE(d.cost_wb2, 0)) as cost_wb,
            COALESCE(d.cost_wb2, 0) as cost_wb2,
            COALESCE(d.cost_wp, 0) as cost_wp,
            COALESCE(d.cost_climate, 0) as cost_climate,
            COALESCE(d.pv_balance_rest, 0) as pv_balance_rest,
            COALESCE(d.bat_balance_rest, 0) as bat_balance_rest,
            COALESCE(d.balance_unknown_rest, 0) as balance_unknown_rest,
            COALESCE(d.pv_e3dc, 0) as pv_e3dc,
            COALESCE(d.pv_external, 0) as pv_external,
            COALESCE(d.pv_source_rest, 0) as pv_source_rest,
            COALESCE(d.pv_grid, 0) as pv_grid,
            COALESCE(d.bat_grid, 0) as bat_grid,
            COALESCE(d.saved_u, 0) as saved_u,
            COALESCE(d.saved_td, 0) as saved_td,
            COALESCE(d.saved_wb, 0) as saved_wb,
            CASE WHEN $realCostDayExpr THEN d.grid_in ELSE 0 END as grid_in_real,
            CASE WHEN $realCostDayExpr THEN d.home_consumption ELSE 0 END as home_real,
            CASE WHEN $realCostDayExpr THEN (COALESCE(d.wb_consumption, 0) + COALESCE(d.wb2_consumption, 0)) ELSE 0 END as wb_real,
            CASE WHEN $realCostDayExpr THEN d.wp_consumption ELSE 0 END as wp_real,
            CASE WHEN $realCostDayExpr THEN COALESCE(d.climate_consumption, 0) ELSE 0 END as climate_real,
            CASE WHEN $realCostDayExpr THEN 0 ELSE d.bat_in END as bat_in_sim
        FROM (SELECT * FROM daily_stats ORDER BY date DESC) d
        ORDER BY d.date DESC";

        $stmtDay = $db->query($queryDay);
        $dailyDataDesc = $stmtDay->fetchAll(PDO::FETCH_ASSOC);
        $dailyData = array_reverse($dailyDataDesc);

// --- NEU: Live-Daten für heute einmischen, um stündliche DB-Verzögerung zu umgehen ---
        $statsFile = '/var/www/html/ramdisk/daily_stats.json';
        if (file_exists($statsFile)) {
            $liveStats = @json_decode(file_get_contents($statsFile), true);
            if ($liveStats && !empty($liveStats['stats'])) {
                $todayStr = date('Y-m-d');
                $foundToday = false;
                foreach ($dailyData as &$row) {
                    if ($row['day'] === $todayStr) {
                        $row['home'] = $liveStats['stats']['total_home_kwh'] ?? $row['home'];
                        $row['pv']   = $liveStats['stats']['total_pv_kwh'] ?? $row['pv'];
                        // Externer Generator (BHKW, 2. PV-Anlage etc.) separat mitmischen
                        $row['ext_pv'] = $liveStats['stats']['total_ext_pv_kwh'] ?? 0;
                        $row['wb1']    = $liveStats['stats']['total_wb_kwh'] ?? ($row['wb1'] ?? $row['wb']);
                        $row['wb2']    = $liveStats['stats']['total_wb2_kwh'] ?? ($row['wb2'] ?? 0);
                        $row['wb']     = (float)$row['wb1'] + (float)$row['wb2'];
                        $row['wp']     = $liveStats['stats']['total_wp_kwh'] ?? $row['wp'];
                        $row['climate'] = $liveStats['stats']['total_climate_kwh'] ?? ($row['climate'] ?? 0);
                        $row['grid_in']  = $liveStats['stats']['total_grid_in_kwh']  ?? $row['grid_in'];
                        $row['grid_out'] = $liveStats['stats']['total_grid_out_kwh'] ?? $liveStats['stats']['pv_grid_kwh'] ?? $row['grid_out'];
                        $row['bat_in']   = $liveStats['stats']['total_bat_in_kwh']   ?? $row['bat_in'];
                        $row['bat_out']  = $liveStats['stats']['total_bat_out_kwh']  ?? $row['bat_out'];
                        $row['pv_e3dc'] = $liveStats['stats']['pv_e3dc_kwh'] ?? ($row['pv_e3dc'] ?? 0);
                        $row['pv_external'] = $liveStats['stats']['pv_external_kwh'] ?? ($row['pv_external'] ?? 0);
                        $row['pv_source_rest'] = $liveStats['stats']['pv_source_rest_kwh'] ?? ($row['pv_source_rest'] ?? 0);
                        $row['pv_grid'] = $liveStats['stats']['pv_grid_kwh'] ?? ($row['pv_grid'] ?? 0);
                        $row['bat_grid'] = $liveStats['stats']['bat_grid_kwh'] ?? ($row['bat_grid'] ?? 0);
                        $balanceRest = lzBuildBalanceRestFromStats(
                            $liveStats['stats'],
                            $row['pv'],
                            $row['grid_in'],
                            $row['grid_out'],
                            $row['bat_in'],
                            $row['bat_out'],
                            $row['home'],
                            $row['wb1'],
                            $row['wb2'],
                            $row['wp'],
                            $row['climate']
                        );
                        $row['pv_balance_rest'] = $balanceRest['pv_balance_rest'];
                        $row['bat_balance_rest'] = $balanceRest['bat_balance_rest'];
                        $row['balance_unknown_rest'] = $balanceRest['balance_unknown_rest'];
                        if (isset($liveStats['costs']) && is_array($liveStats['costs'])) {
                            $row['cost_total'] = $liveStats['costs']['total'] ?? $row['cost_total'];
                            $row['cost_home']  = $liveStats['costs']['home']  ?? $row['cost_home'];
                            $row['cost_wb']    = (float)($liveStats['costs']['wb'] ?? 0) + (float)($liveStats['costs']['wb2'] ?? 0);
                            $row['cost_wb2']   = $liveStats['costs']['wb2']   ?? ($row['cost_wb2'] ?? 0);
                            $row['cost_wp']    = $liveStats['costs']['wp']    ?? $row['cost_wp'];
                            $row['cost_climate'] = $liveStats['costs']['climate'] ?? ($row['cost_climate'] ?? 0);
                        }
                        $savedLive = lzReadLiveSavedFields($liveStats);
                        if ($savedLive['saved_u'] > 0) {
                            $row['saved_u']  = $savedLive['saved_u'];
                            $row['saved_td'] = $savedLive['saved_td'];
                            $row['saved_wb'] = $savedLive['saved_wb'];
                        }
                        lzRefreshDerivedCostFlags($row);
                        lzAddPvTotalFields($row);
                        $foundToday = true;
                        break;
                    }
                }
                unset($row);

                if (!$foundToday) {
                    $savedLive = lzReadLiveSavedFields($liveStats);
                    $newRow = [
                        'day'      => $todayStr,
                        'pv'       => $liveStats['stats']['total_pv_kwh']      ?? 0,
                        'ext_pv'   => $liveStats['stats']['total_ext_pv_kwh']  ?? 0,
                        'home'     => $liveStats['stats']['total_home_kwh']    ?? 0,
                        'wb1'      => $liveStats['stats']['total_wb_kwh']      ?? 0,
                        'wb2'      => $liveStats['stats']['total_wb2_kwh']     ?? 0,
                        'wb'       => (float)($liveStats['stats']['total_wb_kwh'] ?? 0) + (float)($liveStats['stats']['total_wb2_kwh'] ?? 0),
                        'wp'       => $liveStats['stats']['total_wp_kwh']      ?? 0,
                        'climate'  => $liveStats['stats']['total_climate_kwh'] ?? 0,
                        'grid_in'  => $liveStats['stats']['total_grid_in_kwh'] ?? 0,
                        'grid_out' => $liveStats['stats']['total_grid_out_kwh'] ?? $liveStats['stats']['pv_grid_kwh'] ?? 0,
                        'bat_in'   => $liveStats['stats']['total_bat_in_kwh']  ?? 0,
                        'bat_out'  => $liveStats['stats']['total_bat_out_kwh'] ?? 0,
                        'pv_e3dc' => $liveStats['stats']['pv_e3dc_kwh'] ?? 0,
                        'pv_external' => $liveStats['stats']['pv_external_kwh'] ?? 0,
                        'pv_source_rest' => $liveStats['stats']['pv_source_rest_kwh'] ?? 0,
                        'pv_grid' => $liveStats['stats']['pv_grid_kwh'] ?? 0,
                        'bat_grid' => $liveStats['stats']['bat_grid_kwh'] ?? 0,
                        'cost_total' => $liveStats['costs']['total'] ?? 0,
                        'cost_home'  => $liveStats['costs']['home']  ?? 0,
                        'cost_wb'    => (float)($liveStats['costs']['wb'] ?? 0) + (float)($liveStats['costs']['wb2'] ?? 0),
                        'cost_wb2'   => $liveStats['costs']['wb2']   ?? 0,
                        'cost_wp'    => $liveStats['costs']['wp']    ?? 0,
                        'cost_climate' => $liveStats['costs']['climate'] ?? 0,
                        'saved_u'    => $savedLive['saved_u'],
                        'saved_td'   => $savedLive['saved_td'],
                        'saved_wb'   => $savedLive['saved_wb'],
                        'wp_real' => 0, 'bat_in_sim' => 0
                    ];
                    $balanceRest = lzBuildBalanceRestFromStats(
                        $liveStats['stats'],
                        $newRow['pv'],
                        $newRow['grid_in'],
                        $newRow['grid_out'],
                        $newRow['bat_in'],
                        $newRow['bat_out'],
                        $newRow['home'],
                        $newRow['wb1'],
                        $newRow['wb2'],
                        $newRow['wp'],
                        $newRow['climate']
                    );
                    $newRow['pv_balance_rest'] = $balanceRest['pv_balance_rest'];
                    $newRow['bat_balance_rest'] = $balanceRest['bat_balance_rest'];
                    $newRow['balance_unknown_rest'] = $balanceRest['balance_unknown_rest'];
                    lzRefreshDerivedCostFlags($newRow);
                    lzAddPvTotalFields($newRow);
                    $dailyData[] = $newRow;
                }
// Synchronisiere dailyDataDesc für die Tabelle!
                $dailyDataDesc = array_reverse($dailyData);
            }
        }

        // Hole Monatsdaten
        $queryMonth = "SELECT
            strftime('%Y-%m', date) as month,
            SUM(pv_yield) as pv,
            SUM(CASE WHEN COALESCE(grid_out, 0) > 0.5 AND COALESCE(grid_out, 0) > COALESCE(pv_yield, 0) + 0.5 AND COALESCE(grid_out, 0) > MAX(1.0, COALESCE(bat_out, 0) * 1.5) THEN COALESCE(pv_yield, 0) + COALESCE(grid_out, 0) ELSE COALESCE(pv_yield, 0) END) as pv_total,
            SUM(home_consumption) as home,
            SUM(grid_in) as grid_in,
            SUM(grid_out) as grid_out,
            SUM(bat_in) as bat_in,
            SUM(bat_out) as bat_out,
            SUM(COALESCE(wb_consumption, 0)) as wb1,
            SUM(COALESCE(wb2_consumption, 0)) as wb2,
            SUM(COALESCE(wb_consumption, 0) + COALESCE(wb2_consumption, 0)) as wb,
            SUM(wp_consumption) as wp,
            SUM(COALESCE(climate_consumption, 0)) as climate,
            COALESCE(SUM(cost_total), 0) as cost_total,
            COALESCE(SUM(cost_home), 0) as cost_home,
            COALESCE(SUM(cost_wb), 0) + COALESCE(SUM(cost_wb2), 0) as cost_wb,
            COALESCE(SUM(cost_wb2), 0) as cost_wb2,
            COALESCE(SUM(cost_wp), 0) as cost_wp,
            COALESCE(SUM(cost_climate), 0) as cost_climate,
            COALESCE(SUM(pv_balance_rest), 0) as pv_balance_rest,
            COALESCE(SUM(bat_balance_rest), 0) as bat_balance_rest,
            COALESCE(SUM(balance_unknown_rest), 0) as balance_unknown_rest,
            COALESCE(SUM(pv_e3dc), 0) as pv_e3dc,
            COALESCE(SUM(pv_external), 0) as pv_external,
            COALESCE(SUM(pv_source_rest), 0) as pv_source_rest,
            COALESCE(SUM(pv_grid), 0) as pv_grid,
            COALESCE(SUM(bat_grid), 0) as bat_grid,
            COALESCE(SUM(saved_u), 0) as saved_u,
            COALESCE(SUM(saved_td), 0) as saved_td,
            COALESCE(SUM(saved_wb), 0) as saved_wb,
            SUM(CASE WHEN $realCostAggExpr THEN grid_in ELSE 0 END) as grid_in_real,
            SUM(CASE WHEN $realCostAggExpr THEN home_consumption ELSE 0 END) as home_real,
            SUM(CASE WHEN $realCostAggExpr THEN (COALESCE(wb_consumption, 0) + COALESCE(wb2_consumption, 0)) ELSE 0 END) as wb_real,
            SUM(CASE WHEN $realCostAggExpr THEN wp_consumption ELSE 0 END) as wp_real,
            SUM(CASE WHEN $realCostAggExpr THEN COALESCE(climate_consumption, 0) ELSE 0 END) as climate_real,
            SUM(CASE WHEN $realCostAggExpr THEN 0 ELSE bat_in END) as bat_in_sim
        FROM daily_stats
        GROUP BY month
        ORDER BY month ASC";

        $stmtMonth = $db->query($queryMonth);
        $monthlyData = $stmtMonth->fetchAll(PDO::FETCH_ASSOC);

        // Hole Jahresdaten
        $queryYear = "SELECT
            strftime('%Y', date) as year,
            SUM(pv_yield) as pv,
            SUM(CASE WHEN COALESCE(grid_out, 0) > 0.5 AND COALESCE(grid_out, 0) > COALESCE(pv_yield, 0) + 0.5 AND COALESCE(grid_out, 0) > MAX(1.0, COALESCE(bat_out, 0) * 1.5) THEN COALESCE(pv_yield, 0) + COALESCE(grid_out, 0) ELSE COALESCE(pv_yield, 0) END) as pv_total,
            SUM(home_consumption) as home,
            SUM(grid_in) as grid_in,
            SUM(grid_out) as grid_out,
            SUM(bat_in) as bat_in,
            SUM(bat_out) as bat_out,
            SUM(COALESCE(wb_consumption, 0)) as wb1,
            SUM(COALESCE(wb2_consumption, 0)) as wb2,
            SUM(COALESCE(wb_consumption, 0) + COALESCE(wb2_consumption, 0)) as wb,
            SUM(wp_consumption) as wp,
            SUM(COALESCE(climate_consumption, 0)) as climate,
            COALESCE(SUM(cost_total), 0) as cost_total,
            COALESCE(SUM(cost_home), 0) as cost_home,
            COALESCE(SUM(cost_wb), 0) + COALESCE(SUM(cost_wb2), 0) as cost_wb,
            COALESCE(SUM(cost_wb2), 0) as cost_wb2,
            COALESCE(SUM(cost_wp), 0) as cost_wp,
            COALESCE(SUM(cost_climate), 0) as cost_climate,
            COALESCE(SUM(pv_balance_rest), 0) as pv_balance_rest,
            COALESCE(SUM(bat_balance_rest), 0) as bat_balance_rest,
            COALESCE(SUM(balance_unknown_rest), 0) as balance_unknown_rest,
            COALESCE(SUM(pv_e3dc), 0) as pv_e3dc,
            COALESCE(SUM(pv_external), 0) as pv_external,
            COALESCE(SUM(pv_source_rest), 0) as pv_source_rest,
            COALESCE(SUM(pv_grid), 0) as pv_grid,
            COALESCE(SUM(bat_grid), 0) as bat_grid,
            COALESCE(SUM(saved_u), 0) as saved_u,
            COALESCE(SUM(saved_td), 0) as saved_td,
            COALESCE(SUM(saved_wb), 0) as saved_wb,
            SUM(CASE WHEN $realCostAggExpr THEN grid_in ELSE 0 END) as grid_in_real,
            SUM(CASE WHEN $realCostAggExpr THEN home_consumption ELSE 0 END) as home_real,
            SUM(CASE WHEN $realCostAggExpr THEN (COALESCE(wb_consumption, 0) + COALESCE(wb2_consumption, 0)) ELSE 0 END) as wb_real,
            SUM(CASE WHEN $realCostAggExpr THEN wp_consumption ELSE 0 END) as wp_real,
            SUM(CASE WHEN $realCostAggExpr THEN COALESCE(climate_consumption, 0) ELSE 0 END) as climate_real,
            SUM(CASE WHEN $realCostAggExpr THEN 0 ELSE bat_in END) as bat_in_sim
        FROM daily_stats
        GROUP BY year
        ORDER BY year ASC";

        $stmtYear = $db->query($queryYear);
        $yearlyData = $stmtYear->fetchAll(PDO::FETCH_ASSOC);

        lzAddPvTotalFieldsToRows($dailyData);
        lzAddPvTotalFieldsToRows($dailyDataDesc);
        lzAddPvTotalFieldsToRows($monthlyData);
        lzAddPvTotalFieldsToRows($yearlyData);

    } catch (Exception $e) {
        $dbError = $e->getMessage();
    }
} else {
    $dbError = "Datenbank nicht gefunden. Sammelt der Archivar bereits Daten?";
}

$chartDataDayJson = json_encode($dailyData);
$chartDataMonthJson = json_encode($monthlyData);
$chartDataYearJson = json_encode($yearlyData);

// Lese den Basis-Strompreis aus der e3dc.strompreise.txt als Default für die Simulation
$defaultSimulationPrice = 30.0;
$strompreiseFile = '/var/www/html/e3dc.strompreise.txt';
if (file_exists($strompreiseFile)) {
    $lines = @file($strompreiseFile, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    if ($lines !== false) {
        foreach ($lines as $line) {
            $line = trim($line);
            if (empty($line) || str_starts_with($line, '#')) continue;
            $parts = preg_split('/\s+/', $line);
            // Nimmt den ersten validen Preis, was bei statischen Tarifen reicht
            if (count($parts) >= 2 && is_numeric($parts[1])) {
                $defaultSimulationPrice = (float)str_replace(',', '.', $parts[1]);
                break;
            }
        }
    }
}

$lzV4Config = lzReadV4Config();
$lzEegConfig = lzBuildEegConfig($lzV4Config);
$climateEnabled = lzConfigBool($lzV4Config, 'climate_enable', false);
foreach ([$dailyData, $monthlyData, $yearlyData] as $rows) {
    foreach ($rows as $row) {
        if ((float)($row['climate'] ?? 0) > 0.05) {
            $climateEnabled = true;
            break 2;
        }
    }
}
?>

<style>
    .lz-balance-grid {
        display: grid;
        grid-template-columns: minmax(0, 1.35fr) minmax(280px, 0.65fr);
        gap: 1rem;
        margin-bottom: 1.25rem;
    }
    .lz-balance-card {
        border: 1px solid var(--bs-border-color);
        border-radius: 10px;
        background: var(--bs-tertiary-bg);
        padding: 1rem;
    }
    .lz-balance-head {
        display: flex;
        justify-content: space-between;
        gap: .75rem;
        color: var(--bs-secondary-color);
        font-size: .82rem;
        font-weight: 700;
        letter-spacing: 0;
        margin-bottom: .45rem;
    }
    .lz-balance-head span:first-child { text-transform: uppercase; }
    .lz-stack {
        display: flex;
        min-height: 42px;
        overflow: hidden;
        border: 1px solid var(--bs-border-color);
        border-radius: 7px;
        background: rgba(127, 127, 127, .09);
    }
    .lz-stack-seg {
        min-width: 0;
        display: flex;
        align-items: center;
        justify-content: center;
        color: #061013;
        font-size: .78rem;
        font-weight: 800;
        white-space: nowrap;
        overflow: hidden;
        padding: 0 .35rem;
        text-overflow: clip;
    }
    .lz-stack-seg.is-dark {
        color: #fff;
    }
    .lz-balance-legend {
        display: flex;
        flex-wrap: wrap;
        gap: .45rem .85rem;
        margin: .55rem 0 1rem;
        color: var(--bs-secondary-color);
        font-size: .82rem;
    }
    .lz-balance-legend:empty { display: none; }
    .lz-balance-legend strong { color: var(--bs-body-color); }
    .lz-dot {
        display: inline-block;
        width: .65rem;
        height: .65rem;
        border-radius: 50%;
        margin-right: .35rem;
        vertical-align: -.05rem;
    }
    .lz-rank-row {
        display: grid;
        grid-template-columns: minmax(82px, 112px) minmax(0, 1fr) 58px;
        align-items: center;
        gap: .55rem;
        margin: .55rem 0;
        font-size: .88rem;
    }
    .lz-rank-track {
        height: 16px;
        border-radius: 5px;
        background: rgba(127, 127, 127, .13);
        overflow: hidden;
    }
    .lz-rank-fill {
        height: 100%;
        border-radius: 5px;
    }
    .lz-detail-table-wrap {
        max-height: 370px;
        overflow: auto;
        border: 1px solid var(--bs-border-color);
        border-radius: 8px;
    }
    .lz-detail-table {
        margin-bottom: 0;
        min-width: 1060px;
    }
    .lz-detail-table thead th {
        position: sticky;
        top: 0;
        z-index: 1;
        background: var(--bs-tertiary-bg);
    }
    @media (max-width: 820px) {
        .lz-balance-grid {
            grid-template-columns: 1fr;
        }
        .lz-stack { min-height: 38px; }
        .lz-stack-seg { padding: 0 .2rem; font-size: .7rem; }
        .lz-balance-legend { gap: .35rem .7rem; font-size: .76rem; }
    }
</style>

<div class="card dashboard-card shadow-sm mb-4" style="border-radius: 12px; padding: 20px;">
    <div class="d-flex justify-content-between align-items-center mb-4">
        <h5 class="fw-bold m-0" style="color: var(--text-body, inherit);"><i class="fas fa-calendar-alt text-secondary me-2"></i>Langzeit-Bilanzen</h5>
        <div class="d-flex align-items-center gap-2">
            <input type="date" id="lz-date-picker" class="form-control form-control-sm border-secondary-subtle fw-bold text-center" style="width: auto; max-width: 140px; display: none; background-color: var(--bs-body-bg);" onchange="updateFilters()">
            <input type="month" id="lz-month-picker" class="form-control form-control-sm border-secondary-subtle fw-bold text-center" style="width: auto; max-width: 145px; display: none; background-color: var(--bs-body-bg);" onchange="updateFilters()">
            <select id="lz-year-picker" class="form-select form-select-sm border-secondary-subtle fw-bold text-center" style="width: auto; min-width: 110px; display: none; background-color: var(--bs-body-bg);" onchange="updateFilters()">
                <option value="all">Alle Jahre</option>
            </select>

            <div class="btn-group btn-group-sm" role="group">
                <input type="radio" class="btn-check" name="btnradio_lz" id="btn-day" autocomplete="off" checked onchange="switchLangzeitView('day')">
                <label class="btn btn-outline-secondary" for="btn-day">Tage</label>

                <input type="radio" class="btn-check" name="btnradio_lz" id="btn-month" autocomplete="off" onchange="switchLangzeitView('month')">
                <label class="btn btn-outline-secondary" for="btn-month">Monate</label>

                <input type="radio" class="btn-check" name="btnradio_lz" id="btn-year" autocomplete="off" onchange="switchLangzeitView('year')">
                <label class="btn btn-outline-secondary" for="btn-year">Jahre</label>
            </div>
        </div>
    </div>

    <!-- NEU: Summary Header (CO2, Autarkie, Kosten) -->
    <div class="row g-3 mb-4 justify-content-center align-items-stretch" id="lz-summary-header">
        <!-- CO2-Baum -->
        <div class="col-md-2 text-center d-flex flex-column justify-content-center bg-body-tertiary rounded-3 p-3 shadow-sm border border-secondary-subtle mx-1" style="min-width: 150px;">
                        <h6 class="text-muted small text-uppercase fw-bold mb-2">Energiebilanz</h6>
            <div class="d-flex flex-column align-items-center justify-content-center flex-grow-1">
                <div id="co2-tree" class="text-success" style="font-size: 2.5rem; line-height: 1; transition: all 0.5s ease;" title="Der Baum wächst mit deiner Autarkie!"><i class="fas fa-seedling"></i></div>
                <div class="mt-1">
                    <span id="stat-co2-value" class="fw-bold text-success" style="font-size: 1.2rem;">--</span>
                    <span class="text-muted" style="font-size: 0.7rem;"> kg</span>
                </div>
                <div class="text-muted" style="font-size: 0.6rem;">CO<sub>2</sub> gespart</div>
            </div>
        </div>

        <!-- Autarkie Ring -->
        <div class="col-md-2 text-center bg-body-tertiary rounded-3 p-3 shadow-sm border border-secondary-subtle mx-1" style="min-width: 150px;">
            <h6 class="text-muted small text-uppercase fw-bold mb-2">Autarkie</h6>
            <div style="height: 100px; position: relative;">
                <canvas id="chartAutarky"></canvas>
                <div class="position-absolute top-50 start-50 translate-middle text-center" style="pointer-events: none;">
                    <span class="fs-5 fw-bold text-success" id="stat-lz-autarky">--%</span>
                </div>
            </div>
        </div>

        <!-- Eigenverbrauch Ring -->
        <div class="col-md-2 text-center bg-body-tertiary rounded-3 p-3 shadow-sm border border-secondary-subtle mx-1" style="min-width: 150px;">
            <h6 class="text-muted small text-uppercase fw-bold mb-2">Eigenverbrauch</h6>
            <div style="height: 100px; position: relative;">
                <canvas id="chartSelfcon"></canvas>
                <div class="position-absolute top-50 start-50 translate-middle text-center" style="pointer-events: none;">
                    <span class="fs-5 fw-bold text-warning" id="stat-lz-selfcon">--%</span>
                </div>
            </div>
        </div>

        <!-- Speicher Wirkungsgrad (ETA) -->
        <div class="col-md-2 text-center d-flex flex-column justify-content-center bg-body-tertiary rounded-3 p-3 shadow-sm border border-secondary-subtle mx-1" style="min-width: 130px;">
            <h6 class="text-muted small text-uppercase fw-bold mb-2" title="Wirkungsgrad des Speichers (&eta;)">Speicher-ETA (&eta;)</h6>
            <div class="d-flex flex-column align-items-center justify-content-center flex-grow-1">
                <div class="mt-1">
                    <span id="stat-lz-eta" class="fw-bold text-info" style="font-size: 1.6rem; text-shadow: 0 0 10px rgba(13, 202, 240, 0.3);">--%</span>
                </div>
                <div class="text-muted mt-2" style="font-size: 0.65rem;">Entladung / Ladung</div>
                <div class="text-muted" style="font-size: 0.6rem;" id="stat-lz-eta-details">--</div>
            </div>
        </div>

        <!-- Kosten / Finanzielle Bilanz -->
        <div class="col-md-4 mx-1">
            <div class="card bg-body-tertiary border-primary h-100 shadow-sm border-opacity-50">
                <div class="card-body py-2">
                    <h6 class="fw-bold text-success border-bottom border-success border-opacity-25 pb-1 mb-2 d-flex justify-content-between align-items-center">
                        <span><i class="fas fa-euro-sign me-1"></i>Endergebnis</span>
                        <span id="stat-result-total-lz" class="badge bg-success text-body fs-6">0,00 &euro;</span>
                    </h6>
                    <div class="d-flex justify-content-between small mb-1 fw-info"><span>Bezug & Einspeisung:</span> <span id="stat-cost-total-lz" class="text-danger fw-bold">0,00 &euro;</span></div>
                    <div class="d-flex justify-content-between small mb-2 fw-info"><span>Summe der Ersparnis:</span> <span id="stat-save-total-lz" class="text-info fw-bold">0,00 &euro;</span></div>
                    <div class="d-flex justify-content-between small mb-1"><span>EEG-Einspeisevergleich:</span> <span id="stat-eeg-total-lz" class="text-success fw-bold">--</span></div>
                    <div id="stat-eeg-note-lz" class="small text-muted mb-2" style="font-size: 0.7rem;"></div>

                    <div class="row g-0 small text-muted">
                        <div class="col px-1 border-end border-secondary border-opacity-10">
                            <div style="font-size: 0.65rem;" class="text-uppercase fw-bold"><i class="fas fa-home me-1"></i>Haus</div>
                            <div id="lz-cost-home" class="fw-bold text-body">0,00 &euro;</div>
                            <div id="lz-save-home" class="text-info" style="font-size: 0.75rem;">+ 0,00 &euro;</div>
                        </div>
                        <?php if($wbEnabled): ?>
                        <div class="col px-2 border-end border-secondary border-opacity-10">
                            <div style="font-size: 0.65rem;" class="text-uppercase fw-bold"><i class="fas fa-charging-station me-1"></i>Wallbox</div>
                            <div id="lz-cost-wb" class="fw-bold text-body">0,00 &euro;</div>
                            <div id="lz-save-wb" class="text-info" style="font-size: 0.75rem;">+ 0,00 &euro;</div>
                        </div>
                        <?php endif; ?>
                        <?php if($wpEnabled): ?>
                        <div class="col px-2 border-end border-secondary border-opacity-10">
                            <div style="font-size: 0.65rem;" class="text-uppercase fw-bold"><i class="fas fa-fire me-1"></i>WP</div>
                            <div id="lz-cost-wp" class="fw-bold text-body">0,00 &euro;</div>
                            <div id="lz-save-wp" class="text-info" style="font-size: 0.75rem;">+ 0,00 &euro;</div>
                        </div>
                        <?php endif; ?>
                        <?php if($climateEnabled): ?>
                        <div class="col px-2">
                            <div style="font-size: 0.65rem;" class="text-uppercase fw-bold"><i class="fas fa-snowflake me-1"></i>Klima</div>
                            <div id="lz-cost-climate" class="fw-bold text-body">0,00 &euro;</div>
                            <div id="lz-save-climate" class="text-info" style="font-size: 0.75rem;">+ 0,00 &euro;</div>
                        </div>
                        <?php endif; ?>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <div class="row align-items-center mb-4">
        <div class="col-12 col-lg-5">
            <details class="small text-muted">
                <summary class="fw-bold text-body" style="cursor:pointer;">
                    <i class="fas fa-calculator text-success me-1"></i> Bewertung / Alt-Daten
                </summary>
                <div class="input-group input-group-sm shadow-sm mt-2">
                    <span class="input-group-text bg-body-tertiary border-secondary-subtle"><i class="fas fa-euro-sign text-success"></i>&nbsp; Fallback-Strompreis für Altwerte</span>
                    <input type="number" step="0.1" id="lz-price-input" class="form-control border-secondary-subtle fw-bold text-center" value="<?= number_format($defaultSimulationPrice, 1, '.', '') ?>" onchange="updateLangzeitCosts()" onkeyup="updateLangzeitCosts()">
                    <span class="input-group-text bg-body-tertiary border-secondary-subtle">ct/kWh</span>
                </div>
                <div class="mt-1">Wird nur für Zeiträume ohne gespeicherte Kostendaten simuliert. Neue Daten nutzen die hinterlegten Tarife.</div>
            </details>
        </div>
    </div>

    <?php if ($dbError): ?>
        <div class="alert alert-warning">
            <i class="fas fa-exclamation-triangle me-2"></i> <?= htmlspecialchars($dbError) ?>
        </div>
    <?php else: ?>
        <div class="lz-balance-grid">
            <section class="lz-balance-card">
                <div class="d-flex justify-content-between align-items-center gap-3 mb-3">
                    <div>
            <h6 class="text-muted small text-uppercase fw-bold mb-2">Energiebilanz</h6>
                        <div class="small text-muted" id="lz-balance-period">--</div>
                    </div>
                    <div class="btn-group btn-group-sm" role="group" aria-label="Detaildarstellung">
                        <input type="radio" class="btn-check" name="lz_balance_mode" id="lz-balance-stack" autocomplete="off" checked onchange="renderLangzeitBalance()">
                        <label class="btn btn-outline-secondary" for="lz-balance-stack">Bilanz</label>
                        <input type="radio" class="btn-check" name="lz_balance_mode" id="lz-balance-single" autocomplete="off" onchange="renderLangzeitBalance()">
                        <label class="btn btn-outline-secondary" for="lz-balance-single">Einzel</label>
                    </div>
                </div>
                <div id="lz-balance-bars"></div>
            </section>

            <section class="lz-balance-card">
                <div class="d-flex justify-content-between align-items-center gap-3 mb-3">
                    <h6 class="fw-bold m-0" style="color: var(--text-body, inherit);">Zeitraum-Ranking</h6>
                    <span class="small text-muted" id="lz-ranking-total">--</span>
                </div>
                <div id="lz-ranking"></div>
            </section>
        </div>

        <div style="position: relative; height: 40vh; min-height: 350px; width: 100%;">
            <canvas id="langzeitChart"></canvas>
        </div>

        <div class="mt-4">
            <div class="d-flex justify-content-between align-items-center gap-3 mb-2">
                <h6 class="fw-bold m-0" style="color: var(--text-body, inherit);">Details</h6>
                <span class="small text-muted" id="lz-detail-limit-note">--</span>
            </div>
            <div class="lz-detail-table-wrap">
                <table class="table table-sm table-hover text-center lz-detail-table" id="lz-detail-table" style="color: var(--text-body, inherit); font-size: 0.9rem;">
                    <thead>
                        <tr>
                            <th>Zeitraum</th>
                            <th class="text-warning">PV</th>
	                            <th class="text-info">Haus</th>
	                            <th class="text-warning">WP</th>
	                            <th class="text-info">Klima</th>
	                            <th class="text-primary">WB1</th>
                            <th class="text-primary">WB2</th>
                            <th class="text-danger">Netzbezug</th>
                            <th class="text-success">Einspeisung</th>
                            <th>Akku rein</th>
                            <th>Akku raus</th>
                            <th class="text-secondary" style="width: 94px;">Aktionen</th>
                        </tr>
                    </thead>
                    <tbody id="lz-detail-body"></tbody>
                </table>
            </div>
        </div>
    <?php endif; ?>
</div>
<script src="assets/vendor/chart.js/chart.umd.min.js"></script>

<script>
let langzeitChart = null;
const dayData = <?= $chartDataDayJson ?: '[]' ?>;
const monthData = <?= $chartDataMonthJson ?: '[]' ?>;
const yearData = <?= $chartDataYearJson ?: '[]' ?>;
const lzEegConfig = <?= json_encode($lzEegConfig, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE) ?: '{}' ?>;
const lzCsrfToken = <?= json_encode(e3dcCsrfToken()) ?>;
window.UI_ENERGY_FLOW = window.UI_ENERGY_FLOW || <?= json_encode(function_exists('getEnergyFlowUiConfig') ? getEnergyFlowUiConfig() : ['colors' => []], JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE) ?>;

function lzFlowColor(key, fallback) {
    const colors = (window.UI_ENERGY_FLOW && window.UI_ENERGY_FLOW.colors) ? window.UI_ENERGY_FLOW.colors : {};
    const value = String(colors[key] || fallback || '#6c757d').trim();
    return /^#[0-9a-fA-F]{6}$/.test(value) ? value : fallback;
}
const wpEnabled = <?= $wpEnabled ? 'true' : 'false' ?>;
const climateEnabled = <?= $climateEnabled ? 'true' : 'false' ?>;
let currentViewMode = 'day';

function lzEegActive() {
    return Boolean(lzEegConfig && lzEegConfig.enabled && lzEegConfig.in_support && Number(lzEegConfig.tariff_ct) > 0);
}

function lzEegRevenue(gridOutKwh) {
    return lzEegActive() ? ((parseFloat(gridOutKwh) || 0) * Number(lzEegConfig.tariff_ct) / 100) : 0;
}

document.addEventListener("DOMContentLoaded", function() {

    // Achsen-Farben auswerten: HTML (Desktop) und Body (Mobile)
    const getThemeColors = () => {
        const isDarkMode = document.documentElement.getAttribute('data-bs-theme') === 'dark' ||
                           document.body.getAttribute('data-bs-theme') === 'dark' ||
                           document.body.getAttribute('data-theme') === 'dark';
        return {
            textColor: isDarkMode ? '#e0e0e0' : '#333333',
            gridColor: isDarkMode ? 'rgba(255, 255, 255, 0.1)' : 'rgba(0, 0, 0, 0.1)'
        };
    };

// Initialisiere Jahr-Filter Optionen und setze Defaults für die Kalender
    function initYearFilter() {
        const filter = document.getElementById('lz-year-picker');
        if (!filter) return;
        const years = yearData.map(d => d.year).sort((a,b) => b-a);
        years.forEach(y => {
            const opt = document.createElement('option');
            opt.value = y;
            opt.innerText = y;
            filter.appendChild(opt);
        });

// Defaults für Kalender setzen (heute / aktueller Monat)
        const dateInput = document.getElementById('lz-date-picker');
        const monthFilter = document.getElementById('lz-month-picker');

        if (monthFilter && monthData && monthData.length > 0) {
            const monthsAsc = monthData.map(d => d.month).sort((a,b) => a.localeCompare(b));
            monthFilter.min = monthsAsc[0];
            monthFilter.max = monthsAsc[monthsAsc.length - 1];
            monthFilter.value = monthFilter.max;
        }

        if (years.length > 0) filter.value = years[0];

        if (dayData && dayData.length > 0) {
            const minStr = dayData[0].day;
            const maxStr = dayData[dayData.length - 1].day;
            dateInput.min = minStr;
            dateInput.max = maxStr;
            dateInput.value = maxStr;
        }
    }
    initYearFilter();

    function lzNum(row, key) {
        return parseFloat(row && row[key]) || 0;
    }

    function lzKwh(value, digits = 1) {
        return (parseFloat(value) || 0).toLocaleString('de-DE', {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits
        }) + ' kWh';
    }

    function lzAttr(value) {
        return String(value ?? '').replace(/[&<>"']/g, char => ({
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;'
        }[char]));
    }

    function lzPeriodLabel(row) {
        if (!row) return '--';
        if (currentViewMode === 'day') {
            const day = String(row.day || '');
            return day.length === 10 ? `${day.substring(8,10)}.${day.substring(5,7)}.${day.substring(0,4)}` : day;
        }
        if (currentViewMode === 'month') return String(row.month || '--');
        return String(row.year || '--');
    }

    function lzCurrentRows() {
        let rawData = currentViewMode === 'day' ? dayData : (currentViewMode === 'month' ? monthData : yearData);
        rawData = Array.isArray(rawData) ? [...rawData] : [];
        const selectedDate = document.getElementById('lz-date-picker').value;
        const selectedMonth = document.getElementById('lz-month-picker').value;
        const selectedYear = document.getElementById('lz-year-picker').value;
        if (currentViewMode === 'day' && selectedDate) {
            rawData = rawData.filter(d => d.day === selectedDate);
        } else if (currentViewMode === 'month' && selectedMonth) {
            rawData = rawData.filter(d => d.month === selectedMonth);
        } else if (currentViewMode === 'year' && selectedYear !== 'all') {
            rawData = rawData.filter(d => d.year == selectedYear);
        }
        return rawData;
    }

    function lzAggregate(rows) {
        const total = {
            pv: 0, ext: 0, gridIn: 0, gridOut: 0, batIn: 0, batOut: 0,
            home: 0, wp: 0, climate: 0, wb1: 0, wb2: 0, heater: 0,
            pvE3dc: 0, pvExternal: 0, pvSourceRest: 0, pvGrid: 0, batGrid: 0,
            gridOutRest: 0, pvBalanceRest: 0, batBalanceRest: 0, balanceUnknownRest: 0, balanceRest: 0, saved: 0
        };
        rows.forEach(d => {
            total.pv += lzNum(d, 'pv_total') || lzNum(d, 'pv');
            total.ext += lzNum(d, 'ext_pv');
            total.gridIn += lzNum(d, 'grid_in');
            total.gridOut += lzNum(d, 'grid_out');
            total.batIn += lzNum(d, 'bat_in');
            total.batOut += lzNum(d, 'bat_out');
            total.home += lzNum(d, 'home');
            total.wp += lzNum(d, 'wp');
            total.climate += lzNum(d, 'climate');
            total.wb1 += lzNum(d, 'wb1') || Math.max(0, lzNum(d, 'wb') - lzNum(d, 'wb2'));
            total.wb2 += lzNum(d, 'wb2');
            total.heater += lzNum(d, 'heizstab') || lzNum(d, 'hs');
            total.pvE3dc += lzNum(d, 'pv_e3dc');
            total.pvExternal += lzNum(d, 'pv_external');
            total.pvSourceRest += lzNum(d, 'pv_source_rest');
            total.pvGrid += lzNum(d, 'pv_grid');
            total.batGrid += lzNum(d, 'bat_grid');
            total.pvBalanceRest += lzNum(d, 'pv_balance_rest');
            total.batBalanceRest += lzNum(d, 'bat_balance_rest');
            total.balanceUnknownRest += lzNum(d, 'balance_unknown_rest');
            total.saved += lzNum(d, 'saved_u');
        });
        const supply = total.pv + total.ext + total.gridIn + total.batOut;
        const knownUse = total.home + total.wp + total.climate + total.wb1 + total.wb2 + total.heater + total.gridOut + total.batIn;
        const broadRest = Math.max(0, supply - knownUse);
        const detailRest = total.pvBalanceRest + total.batBalanceRest + total.balanceUnknownRest;
        if (detailRest <= 0.05 && broadRest > 0.05) {
            total.balanceUnknownRest = broadRest;
        }
        total.balanceRest = total.pvBalanceRest + total.batBalanceRest + total.balanceUnknownRest;
        const pvSourceKnown = total.pvE3dc + total.pvExternal + total.pvSourceRest;
        if (pvSourceKnown <= 0.05 && total.pv > 0.05) {
            total.pvE3dc = total.pv;
            total.pvExternal = 0;
            total.pvSourceRest = 0;
        } else if (pvSourceKnown > total.pv + 0.05 && total.pv > 0.05) {
            const factor = total.pv / pvSourceKnown;
            total.pvE3dc *= factor;
            total.pvExternal *= factor;
            total.pvSourceRest *= factor;
        } else if (total.pv > pvSourceKnown + 0.05) {
            total.pvSourceRest += total.pv - pvSourceKnown;
        }
        total.gridOutRest = Math.max(0, total.gridOut - total.pvGrid - total.batGrid);
        total.knownUse = knownUse;
        total.supply = supply;
        total.use = knownUse + total.balanceRest;
        return total;
    }

    function lzBalanceRestTooltip(sum) {
        return [
            `Bilanzrest gesamt: ${lzKwh(sum.balanceRest)}`,
            `PV/WR: ${lzKwh(sum.pvBalanceRest)} · Batterie: ${lzKwh(sum.batBalanceRest)} · ungeklärt: ${lzKwh(sum.balanceUnknownRest)}`,
            'Das ist kein eigener Verbraucher, sondern Rest aus Zählerwegen, Wandlungsverlusten, Rundung, Zeitversatz und History-Lücken.'
        ].join('\n');
    }

    function lzItemTooltip(item) {
        return item.tooltip || `${item.label}: ${lzKwh(item.value)}`;
    }

    function lzInlineLabel(item, total) {
        if (!(total > 0) || !(item.value > 0.05)) return '';
        const label = `${lzKwh(item.value, item.value >= 10 ? 1 : 2)} ${item.label}`;
        const grid = document.querySelector('.lz-balance-grid');
        const gridWidth = grid?.clientWidth || Math.max(240, Math.min(window.innerWidth - 40, 720));
        const stackWidth = window.matchMedia('(max-width: 820px)').matches
            ? gridWidth
            : Math.max(240, (gridWidth - 20) / 2);
        const canvas = lzInlineLabel.canvas || (lzInlineLabel.canvas = document.createElement('canvas'));
        const context = canvas.getContext('2d');
        context.font = window.matchMedia('(max-width: 820px)').matches
            ? '800 11.2px system-ui, sans-serif'
            : '800 12.48px system-ui, sans-serif';
        const requiredWidth = context.measureText(label).width + 12;
        return (item.value / total) * stackWidth >= requiredWidth ? label : '';
    }

    function lzLegend(items, total) {
        return `<div class="lz-balance-legend">${items
            .filter(item => item.value > 0.05 && !lzInlineLabel(item, total))
            .map(item => `<span title="${lzAttr(lzItemTooltip(item))}"><i class="lz-dot" style="background:${item.color}"></i>${item.label} <strong>${lzKwh(item.value, item.value >= 10 ? 1 : 2)}</strong></span>`)
            .join('')}</div>`;
    }

    function lzStack(items, total) {
        const visible = items.filter(item => item.value > 0.05);
        if (visible.length === 0) return '<div class="text-muted small">Keine Werte</div>';
        return `<div class="lz-stack">${visible.map(item => {
            const width = total > 0 ? item.value / total * 100 : 0;
            const label = lzInlineLabel(item, total);
            return `<div class="lz-stack-seg${item.dark ? ' is-dark' : ''}" style="width:${width.toFixed(3)}%;background:${item.color}" title="${lzAttr(lzItemTooltip(item))}">${label}</div>`;
        }).join('')}</div>`;
    }

    function lzSingleBars(items) {
        const maxValue = Math.max(0.001, ...items.map(item => item.value));
        return items.filter(item => item.value > 0.05).map(item => `
            <div class="lz-rank-row" title="${lzAttr(lzItemTooltip(item))}">
                <span>${item.label}</span>
                <div class="lz-rank-track"><div class="lz-rank-fill" style="width:${Math.max(2, item.value / maxValue * 100).toFixed(2)}%;background:${item.color}"></div></div>
                <strong>${lzKwh(item.value, item.value >= 10 ? 1 : 2).replace(' kWh', '')}</strong>
            </div>
        `).join('');
    }

    window.renderLangzeitBalance = function() {
        const rows = lzCurrentRows();
        const sum = lzAggregate(rows);
        const balanceEl = document.getElementById('lz-balance-bars');
        const periodEl = document.getElementById('lz-balance-period');
        const rankingEl = document.getElementById('lz-ranking');
        const rankingTotalEl = document.getElementById('lz-ranking-total');
        if (!balanceEl || !rankingEl) return;
        periodEl.innerText = rows.length === 1 ? lzPeriodLabel(rows[0]) : `${rows.length} Zeiträume`;

        const supplyItems = [
            {label: 'E3DC-PV', value: sum.pvE3dc, color: lzFlowColor('pv', '#ffc107')},
            {label: 'Zusatz-WR', value: sum.pvExternal, color: '#22c55e'},
            {label: 'PV/WR ungeklärt', value: sum.pvSourceRest, color: '#f59e0b', dark: true},
            {label: 'Extern', value: sum.ext, color: '#8bc34a'},
            {label: 'Netzbezug', value: sum.gridIn, color: lzFlowColor('grid_import', '#ef4444'), dark: true},
            {label: 'Batterieentladung', value: sum.batOut, color: lzFlowColor('battery', '#00a878')}
        ];
        const useItems = [
            {label: 'Haus', value: sum.home, color: lzFlowColor('home', '#00bcd4')},
            {label: 'WP', value: sum.wp, color: lzFlowColor('heatpump', '#ff7a00')},
            {label: 'Klima', value: sum.climate, color: lzFlowColor('climate', '#38bdf8')},
            {label: 'WB1', value: sum.wb1, color: lzFlowColor('wallbox', '#2f80ed'), dark: true},
            {label: 'WB2', value: sum.wb2, color: '#7b61ff', dark: true},
            {label: 'Heizstab', value: sum.heater, color: '#e05555', dark: true},
            {label: 'PV-Einspeisung', value: sum.pvGrid, color: lzFlowColor('pv', '#ffc107')},
            {label: 'Batterie-Verkauf', value: sum.batGrid, color: lzFlowColor('battery', '#00a878')},
            {label: 'Netzeinspeisung Rest', value: sum.gridOutRest, color: '#8a949e', dark: true},
            {label: 'Batterieladung', value: sum.batIn, color: lzFlowColor('battery_charge', '#16b884')},
            {label: 'PV/WR-Bilanzrest', value: sum.pvBalanceRest, color: '#f59e0b', dark: true, tooltip: lzBalanceRestTooltip(sum)},
            {label: 'Batterie-Bilanzrest', value: sum.batBalanceRest, color: '#64748b', dark: true, tooltip: lzBalanceRestTooltip(sum)},
            {label: 'Bilanzrest', value: sum.balanceUnknownRest, color: '#9aa5ac', dark: true, tooltip: lzBalanceRestTooltip(sum)}
        ];
        const singleMode = document.getElementById('lz-balance-single')?.checked;
        const sharedBalanceBasis = Math.max(sum.supply, sum.use, 0.001);
        balanceEl.innerHTML = singleMode
            ? lzSingleBars([...supplyItems, ...useItems])
            : `
                <div class="lz-balance-head"><span>Energielieferung</span><span>${lzKwh(sum.supply)}</span></div>
                ${lzStack(supplyItems, sharedBalanceBasis)}
                ${lzLegend(supplyItems, sharedBalanceBasis)}
                <div class="lz-balance-head mt-3"><span>Energieverwendung</span><span>${lzKwh(sum.use)}</span></div>
                ${lzStack(useItems, sharedBalanceBasis)}
                ${lzLegend(useItems, sharedBalanceBasis)}
            `;

        const rankingItems = useItems
            .filter(item => item.value > 0.05)
            .sort((a, b) => b.value - a.value)
            .slice(0, 7);
        rankingTotalEl.innerText = lzKwh(sum.use);
        rankingEl.innerHTML = lzSingleBars(rankingItems);
        renderLangzeitDetailTable(rows);
    };
    let lzBalanceResizeTimer = null;
    window.addEventListener('resize', () => {
        window.clearTimeout(lzBalanceResizeTimer);
        lzBalanceResizeTimer = window.setTimeout(() => window.renderLangzeitBalance(), 120);
    }, {passive: true});

    function renderLangzeitDetailTable(rows) {
        const tbody = document.getElementById('lz-detail-body');
        const note = document.getElementById('lz-detail-limit-note');
        if (!tbody) return;
        const limit = currentViewMode === 'day' ? 31 : 24;
        const sorted = [...rows].sort((a, b) => String(b.day || b.month || b.year).localeCompare(String(a.day || a.month || a.year)));
        const visible = sorted.slice(0, limit);
        note.innerText = sorted.length > limit ? `${visible.length} von ${sorted.length} angezeigt` : `${visible.length} angezeigt`;
        tbody.innerHTML = '';
        visible.forEach(d => {
            const tr = document.createElement('tr');
            const dayActions = currentViewMode === 'day' && d.day ? `
                <button class="btn btn-sm btn-outline-info lz-edit-row" title="Bearbeiten"><i class="fas fa-edit"></i></button>
                <button class="btn btn-sm btn-outline-danger ms-1 lz-delete-row" title="Löschen"><i class="fas fa-trash-alt"></i></button>
            ` : '';
            tr.innerHTML = `
                <td class="fw-bold text-start">${lzPeriodLabel(d)}</td>
                <td>${lzKwh(lzNum(d, 'pv_total') || lzNum(d, 'pv'))}</td>
	                <td>${lzKwh(lzNum(d, 'home'))}</td>
	                <td>${lzKwh(lzNum(d, 'wp'))}</td>
	                <td>${lzKwh(lzNum(d, 'climate'))}</td>
	                <td>${lzKwh(lzNum(d, 'wb1') || Math.max(0, lzNum(d, 'wb') - lzNum(d, 'wb2')))}</td>
                <td>${lzKwh(lzNum(d, 'wb2'))}</td>
                <td>${lzKwh(lzNum(d, 'grid_in'))}</td>
                <td>${lzKwh(lzNum(d, 'grid_out'))}</td>
                <td>${lzKwh(lzNum(d, 'bat_in'))}</td>
                <td>${lzKwh(lzNum(d, 'bat_out'))}</td>
                <td><div class="d-flex justify-content-center">${dayActions}</div></td>
            `;
            const edit = tr.querySelector('.lz-edit-row');
            const del = tr.querySelector('.lz-delete-row');
            if (edit) edit.addEventListener('click', () => openStatsEditor(d));
            if (del) del.addEventListener('click', () => deleteStatsEntry(d.day));
            tbody.appendChild(tr);
        });
    }

    window.updateFilters = function() {
        renderChart();
        updateLzSummary();
        updateLangzeitCosts();
    };

    // Zentrale Render-Funktion baut das Diagramm frisch auf
    let renderTimeout = null;
    const renderChart = () => {
        if (renderTimeout) clearTimeout(renderTimeout);
        renderTimeout = setTimeout(() => {
            if (langzeitChart) {
                langzeitChart.destroy();
            }

            const ctx = document.getElementById('langzeitChart').getContext('2d');
            const colors = getThemeColors();
            let rawData = lzCurrentRows();

            const labels = rawData.map(d => currentViewMode === 'day' ? (d.day.substring(8,10) + '.' + d.day.substring(5,7) + '.') : (currentViewMode === 'month' ? d.month : d.year));

            // Gespeicherte Sichtbarkeiten aus dem Browser laden
            let hiddenState = {};
            try { hiddenState = JSON.parse(localStorage.getItem('e3dc_lz_hidden')) || {}; } catch(e) {}

            let datasets = [
                { label: 'PV Ertrag', stack: 'pv', data: rawData.map(d => parseFloat(d.pv_total ?? d.pv) || 0), backgroundColor: lzFlowColor('pv', '#e0a800'), borderRadius: 4, hidden: hiddenState['PV Ertrag'] || false },
                { label: 'Hausverbrauch', stack: 'cons', data: rawData.map(d => parseFloat(d.home) || 0), backgroundColor: lzFlowColor('home', '#0a58ca'), borderRadius: 0, hidden: hiddenState['Hausverbrauch'] || false }
            ];

            if (wpEnabled) {
                datasets.push({ label: 'Wärmepumpe', stack: 'cons', data: rawData.map(d => parseFloat(d.wp) || 0), backgroundColor: lzFlowColor('heatpump', '#c2410c'), borderRadius: 0, hidden: hiddenState['Wärmepumpe'] || false });
            }

            if (climateEnabled || rawData.some(d => (parseFloat(d.climate) || 0) > 0.05)) {
                datasets.push({ label: 'Klima', stack: 'cons', data: rawData.map(d => parseFloat(d.climate) || 0), backgroundColor: lzFlowColor('climate', '#38bdf8'), borderRadius: 0, hidden: hiddenState['Klima'] || false });
            }

            datasets.push(
                { label: 'Wallbox', stack: 'cons', data: rawData.map(d => parseFloat(d.wb) || 0), backgroundColor: lzFlowColor('wallbox', '#0f766e'), borderRadius: 0, hidden: hiddenState['Wallbox'] || false },
                { label: 'Netzbezug', stack: 'gridin', data: rawData.map(d => parseFloat(d.grid_in) || 0), backgroundColor: lzFlowColor('grid_import', '#ef4444'), borderRadius: 4, hidden: hiddenState['Netzbezug'] || false },
                { label: 'Einspeisung', stack: 'gridout', data: rawData.map(d => parseFloat(d.grid_out) || 0), backgroundColor: lzFlowColor('grid_export', '#2ecc71'), borderRadius: 4, hidden: hiddenState['Einspeisung'] !== undefined ? hiddenState['Einspeisung'] : true },
                { label: 'Peak gerettet', stack: 'pv', data: rawData.map(d => parseFloat(d.saved_u) || 0), backgroundColor: '#20c997', borderRadius: 4, hidden: hiddenState['Peak gerettet'] || false }
            );



            langzeitChart = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: labels,
                    datasets: datasets
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    color: colors.textColor,
                    plugins: {
                        legend: {
                            position: 'bottom',
                            labels: { color: colors.textColor, padding: 20 },
                            onClick: function(e, legendItem, legend) {
                                const index = legendItem.datasetIndex;
                                const ci = legend.chart;
                                const isHidden = ci.isDatasetVisible(index);
                                if (isHidden) {
                                    ci.hide(index);
                                    hiddenState[legendItem.text] = true;
                                } else {
                                    ci.show(index);
                                    hiddenState[legendItem.text] = false;
                                }
                                legendItem.hidden = isHidden;
                                localStorage.setItem('e3dc_lz_hidden', JSON.stringify(hiddenState));
                            }
                        },
                        tooltip: {
                            mode: 'index', intersect: false,
                            callbacks: {
                                label: function(context) {
                                    let label = context.dataset.label || '';
                                    if (label) label += ': ';
                                    if (context.parsed.y !== null) label += context.parsed.y.toLocaleString('de-DE', {minimumFractionDigits: 1, maximumFractionDigits: 1}) + ' kWh';
                                    return label;
                                }
                            }
                        }
                    },
                    scales: {
                        x: { stacked: true, grid: { color: colors.gridColor, borderColor: colors.gridColor, drawBorder: false }, ticks: { color: colors.textColor, display: rawData.length > 1 } },
                        y: { stacked: true, grid: { color: colors.gridColor, borderColor: colors.gridColor, drawBorder: false }, ticks: { color: colors.textColor }, title: { display: true, text: 'Energie (kWh)', color: colors.textColor } }
                    }
                }
            });
        }, 50); // 50ms Wartezeit faengt alle fast-gleichzeitigen Events sauber ab
    };

    // 1. Lausche auf das explizite Event
    window.addEventListener('themeChanged', function() {
        // Diagramm mit den neuen Farben neu zeichnen
        initLzSummaryCharts();
        renderChart();
    });

    // 2. Kugelsicherer Wachhund (MutationObserver), der greift, falls solar.js das Theme umstellt
    const themeObserver = new MutationObserver(function(mutations) {
        let changed = false;
        mutations.forEach(function(m) {
            if (m.attributeName === 'data-bs-theme' || m.attributeName === 'data-theme') changed = true;
        });
        if (changed) {
            initLzSummaryCharts();
            renderChart();
        }
    });

    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-bs-theme', 'data-theme'] });
    themeObserver.observe(document.body, { attributes: true, attributeFilter: ['data-bs-theme', 'data-theme'] });

    // Lade gespeicherten Preis (ueberschreibt Default falls vorhanden)
    const savedPrice = localStorage.getItem('e3dc_lz_price');
    if (savedPrice) {
        document.getElementById('lz-price-input').value = savedPrice;
    }

    // --- Initialisierung wurde ans Ende verschoben ---

    // --- NEU: LZ Summary Charts & Logic ---
    let chartAutarky = null;
    let chartSelfcon = null;

    function initLzSummaryCharts() {
        const colors = getThemeColors();
        const gridColor = colors.gridColor;

        if (chartAutarky) chartAutarky.destroy();
        if (chartSelfcon) chartSelfcon.destroy();

        const commonOptions = {
            responsive: true, maintainAspectRatio: false, cutout: '75%',
            plugins: { legend: { display: false }, tooltip: { enabled: false } },
            events: []
        };

        const ctxA = document.getElementById('chartAutarky').getContext('2d');
        chartAutarky = new Chart(ctxA, {
            type: 'doughnut',
            data: { labels: ['Autark', 'Netz'], datasets: [{ data: [0, 100], backgroundColor: [lzFlowColor('battery', '#198754'), lzFlowColor('grid', gridColor)], borderWidth: 0 }] },
            options: commonOptions
        });

        const ctxS = document.getElementById('chartSelfcon').getContext('2d');
        chartSelfcon = new Chart(ctxS, {
            type: 'doughnut',
            data: { labels: ['Eigen', 'Netz'], datasets: [{ data: [0, 100], backgroundColor: [lzFlowColor('pv', '#ffc107'), lzFlowColor('grid', gridColor)], borderWidth: 0 }] },
            options: commonOptions
        });
    }

    function updateLzSummary() {
        let rawData = lzCurrentRows();
        if (!rawData || rawData.length === 0) return;

// Summen bilden (inkl. externem Generator für korrekte Autarkie/Eigenverbrauch!)
        let sPV = 0, sHome = 0, sGridIn = 0, sGridOut = 0, sBatOut = 0, sBatIn = 0;
        let sCostTotal = 0, sCostHome = 0, sCostWb = 0, sCostWp = 0, sCostClimate = 0;
        let sGridInReal = 0, sHomeReal = 0, sWbReal = 0, sWpReal = 0, sClimateReal = 0;

        rawData.forEach(d => {
            sPV     += parseFloat(d.pv_total ?? d.pv) || 0;
            sHome   += (parseFloat(d.home)  || 0) + (parseFloat(d.wb) || 0) + (parseFloat(d.wp) || 0) + (parseFloat(d.climate) || 0);
            sGridIn  += parseFloat(d.grid_in)  || 0;
            sGridOut += parseFloat(d.grid_out) || 0;
            sBatOut  += parseFloat(d.bat_out)  || 0;
            sBatIn   += parseFloat(d.bat_in)   || 0;

            sCostTotal += parseFloat(d.cost_total) || 0;
            sCostHome  += parseFloat(d.cost_home)  || 0;
            sCostWb    += parseFloat(d.cost_wb)    || 0;
            sCostWp    += parseFloat(d.cost_wp)    || 0;
            sCostClimate += parseFloat(d.cost_climate) || 0;

            sGridInReal += parseFloat(d.grid_in_real) || 0;
            sHomeReal   += parseFloat(d.home_real)    || 0;
            sWbReal     += parseFloat(d.wb_real)      || 0;
            sWpReal     += parseFloat(d.wp_real)      || 0;
            sClimateReal += parseFloat(d.climate_real) || 0;
        });

        // Autarkie & Eigenverbrauch mit dem sichtbaren Gesamt-PV-Ertrag.
        // Autarkie   = (Gesamtverbrauch - Netzbezug) / Gesamtverbrauch
        // Eigenverbr = (PV gesamt - Einspeisung) / PV gesamt
        const sTotalProduction = sPV;
        const autarky = sHome > 0.1 ? Math.min(100, Math.max(0, (1 - sGridIn / sHome) * 100)) : 0;
        const selfcon = sTotalProduction > 0.1 ? Math.min(100, Math.max(0, (1 - sGridOut / sTotalProduction) * 100)) : 0;

        document.getElementById('stat-lz-autarky').innerText = autarky.toFixed(1) + '%';
        document.getElementById('stat-lz-selfcon').innerText = selfcon.toFixed(1) + '%';

        // Speicher-Wirkungsgrad (ETA)
        const eta = sBatIn > 0 ? Math.min(100, (sBatOut / sBatIn) * 100) : 0;
        const etaEl = document.getElementById('stat-lz-eta');
        if (etaEl) {
            etaEl.innerText = sBatIn > 0 ? eta.toFixed(1) + '%' : '--%';
            const detailsEl = document.getElementById('stat-lz-eta-details');
            if (detailsEl) {
                if (sBatIn > 0) {
                    detailsEl.innerText = sBatOut.toFixed(0) + ' / ' + sBatIn.toFixed(0) + ' kWh';
                } else {
                    detailsEl.innerText = '--';
                }
            }
        }

        if (chartAutarky) {
            chartAutarky.data.datasets[0].data = [autarky, 100 - autarky];
            chartAutarky.update();
        }
        if (chartSelfcon) {
            chartSelfcon.data.datasets[0].data = [selfcon, 100 - selfcon];
            chartSelfcon.update();
        }

        // CO2-Ersparnis: (Home - Netzbezug) * 0.38 kg/kWh
        const co2Saved = Math.max(0, (sHome - sGridIn) * 0.38);
        document.getElementById('stat-co2-value').innerText = co2Saved.toLocaleString('de-DE', {minimumFractionDigits: 1, maximumFractionDigits: 1});

        // Baum-Icon waechst
        const treeEl = document.getElementById('co2-tree');
        if (treeEl) {
            let icon, size;
            if (autarky >= 95)      { icon = 'fa-tree';     size = '2.2rem'; }
            else if (autarky >= 80) { icon = 'fa-tree';     size = '2.4rem'; }
            else if (autarky >= 60) { icon = 'fa-tree';     size = '2.8rem'; }
            else if (autarky >= 40) { icon = 'fa-seedling'; size = '2.5rem'; }
            else if (autarky >= 20) { icon = 'fa-leaf';     size = '2.5rem'; }
            else                    { icon = 'fa-seedling'; size = '2.5rem'; }
            treeEl.innerHTML = '<i class="fas ' + icon + '"></i>';
            treeEl.style.fontSize = size;
        }

        // Kosten & Ersparnis (Simulierte Anteile dazurechnen)
        const priceCt = parseFloat(document.getElementById('lz-price-input').value) || 0;
        const priceEur = priceCt / 100;

        const gridSim = Math.max(0, sGridIn - sGridInReal);
        const homeSim = Math.max(0, (sHome - sWbReal - sWpReal - sClimateReal) - (sHomeReal - sWbReal - sWpReal - sClimateReal)); // Vereinfacht
        // Besser: Nutze Anteile wie in updateLangzeitCosts
        // Da wir hier aber globale Summen haben, machen wir es proportional

        let totalCost = sCostTotal + (gridSim * priceEur);
        let totalSave = Math.max(0, (sHome - sGridIn) * priceEur);
        const eegRevenue = lzEegRevenue(sGridOut);
        let finalResult = totalSave + eegRevenue - totalCost;

        document.getElementById('stat-cost-total-lz').innerText = totalCost.toLocaleString('de-DE', {style: 'currency', currency: 'EUR'});
        document.getElementById('stat-save-total-lz').innerText = totalSave.toLocaleString('de-DE', {style: 'currency', currency: 'EUR'});
        document.getElementById('stat-result-total-lz').innerText = finalResult.toLocaleString('de-DE', {style: 'currency', currency: 'EUR'});
        const eegTotalEl = document.getElementById('stat-eeg-total-lz');
        const eegNoteEl = document.getElementById('stat-eeg-note-lz');
        if (eegTotalEl && eegNoteEl) {
            if (lzEegActive()) {
                eegTotalEl.innerText = '+ ' + eegRevenue.toLocaleString('de-DE', {style: 'currency', currency: 'EUR'});
                const tariffText = Number(lzEegConfig.tariff_ct).toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 3});
                const gridText = sGridOut.toLocaleString('de-DE', {minimumFractionDigits: 1, maximumFractionDigits: 1});
                const supportText = lzEegConfig.support_until ? `, Förderung bis ${lzEegConfig.support_until}` : '';
                const sourceText = lzEegConfig.rate_source && lzEegConfig.rate_source !== 'manual' ? ', BNetzA-Tabelle' : '';
                const warningText = lzEegConfig.source_warning ? ` ${lzEegConfig.source_warning}` : '';
                eegNoteEl.innerText = `${gridText} kWh x ${tariffText} ct/kWh${supportText}${sourceText}.${warningText}`;
            } else if (lzEegConfig && lzEegConfig.enabled) {
                eegTotalEl.innerText = '--';
                eegNoteEl.innerText = 'EEG aktiv, aber Vergütungsstufen oder Förderzeitraum sind nicht berechenbar.';
            } else {
                eegTotalEl.innerText = '--';
                eegNoteEl.innerText = '';
            }
        }

        // Split (Proportional zum Verbrauch)
        const sWb = rawData.reduce((a, b) => a + (parseFloat(b.wb)||0), 0);
        const sWp = rawData.reduce((a, b) => a + (parseFloat(b.wp)||0), 0);
        const sClimate = rawData.reduce((a, b) => a + (parseFloat(b.climate)||0), 0);
        const sHomeOnly = sHome - sWb - sWp - sClimate;

        const updateSplit = (idCost, idSave, consumption, realCost) => {
            const elCost = document.getElementById(idCost);
            const elSave = document.getElementById(idSave);
            if (!elCost || !elSave) return;

            const ratio = sHome > 0 ? consumption / sHome : 0;
            const cost = realCost + (gridSim * ratio * priceEur);
            const save = Math.max(0, (consumption - (sGridIn * ratio)) * priceEur);
            elCost.innerText = cost.toLocaleString('de-DE', {style: 'currency', currency: 'EUR'});
            elSave.innerText = '+ ' + save.toLocaleString('de-DE', {style: 'currency', currency: 'EUR'});
        };

        updateSplit('lz-cost-home', 'lz-save-home', sHomeOnly, sCostHome);
        updateSplit('lz-cost-wb', 'lz-save-wb', sWb, sCostWb);
        updateSplit('lz-cost-wp', 'lz-save-wp', sWp, sCostWp);
        updateSplit('lz-cost-climate', 'lz-save-climate', sClimate, sCostClimate);
    }

    // Dynamische Kosten-Berechnung
    window.updateLangzeitCosts = function() {
        const priceCt = parseFloat(document.getElementById('lz-price-input').value) || 0;
        localStorage.setItem('e3dc_lz_price', priceCt);
        updateLzSummary();
        renderLangzeitBalance();
    };
    // Umschaltung Monat / Jahr
    window.switchLangzeitView = function(view) {
        currentViewMode = view;
        document.getElementById('lz-date-picker').style.display = (view === 'day') ? 'block' : 'none';
        document.getElementById('lz-month-picker').style.display = (view === 'month') ? 'block' : 'none';
        document.getElementById('lz-year-picker').style.display = (view === 'year') ? 'block' : 'none';
        renderChart();
        updateLangzeitCosts();
    };
    setTimeout(updateLangzeitCosts, 300);

    // --- Statistik Editor Logic ---
    const statsModal = new bootstrap.Modal(document.getElementById('statsEditorModal'));

    function lzFormatEditKwh(value) {
        if (value === null || value === undefined || value === '') return '';
        const numberValue = Number(String(value).replace(',', '.'));
        if (!Number.isFinite(numberValue)) return value;
        const rounded = Math.round((numberValue + Number.EPSILON) * 100) / 100;
        return Object.is(rounded, -0) || rounded === 0 ? '0' : rounded.toFixed(2);
    }

    window.openStatsEditor = function(data) {
        document.getElementById('edit-date').value = data.day;
        document.getElementById('edit-pv').value = lzFormatEditKwh(data.pv_total ?? data.pv);
        document.getElementById('edit-home').value = lzFormatEditKwh(data.home);
        document.getElementById('edit-grid-in').value = lzFormatEditKwh(data.grid_in);
        document.getElementById('edit-grid-out').value = lzFormatEditKwh(data.grid_out);
        document.getElementById('edit-bat-in').value = lzFormatEditKwh(data.bat_in);
        document.getElementById('edit-bat-out').value = lzFormatEditKwh(data.bat_out);
        document.getElementById('edit-wb').value = lzFormatEditKwh(data.wb1 ?? data.wb);
        document.getElementById('edit-wb2').value = lzFormatEditKwh(data.wb2 ?? 0);
        document.getElementById('edit-wp').value = lzFormatEditKwh(data.wp);
        document.getElementById('edit-climate').value = lzFormatEditKwh(data.climate);

        const isToday = (data.day === new Date().toISOString().substring(0, 10));
        let warningEl = document.getElementById('edit-today-warning');
        if (!warningEl) {
            warningEl = document.createElement('div');
            warningEl.id = 'edit-today-warning';
            warningEl.className = 'alert alert-danger mt-3 py-2 small';
            warningEl.innerHTML = '<i class="fas fa-exclamation-triangle me-1"></i> Der heutige Tag kann nicht manuell gespeichert werden, da Live-Daten & Hintergrunddienste ihn ständig überschreiben.';
            const formObj = document.getElementById('statsEditorForm');
            formObj.parentNode.insertBefore(warningEl, formObj.nextSibling);
        }
        warningEl.style.display = isToday ? 'block' : 'none';
        document.getElementById('btn-save-stats').disabled = isToday;

        statsModal.show();
    };

    window.saveStatsEntry = function() {
        const formData = new FormData(document.getElementById('statsEditorForm'));
        formData.append('action', 'save');

        fetch('<?= getAjaxActionUrl("langzeit") ?>', {
            method: 'POST',
            body: formData
        })
        .then(async r => {
            let payload = null;
            try { payload = await r.json(); } catch (_) { payload = null; }
            if (!r.ok || !payload || payload.success !== true) {
                throw new Error(payload && payload.error ? String(payload.error) : ('HTTP ' + r.status));
            }
            return payload;
        })
        .then(res => {
            setTimeout(() => { location.reload(); }, 120);
        })
        .catch(error => alert("Fehler beim Speichern: " + error.message));
    };

    window.deleteStatsEntry = function(date) {
        if (!confirm("Eintrag für " + date + " wirklich löschen?")) return;

        const formData = new FormData();
        formData.append('date', date);
        formData.append('action', 'delete');
        formData.append('csrf_token', lzCsrfToken);

        fetch('<?= getAjaxActionUrl("langzeit") ?>', {
            method: 'POST',
            body: formData
        })
        .then(async r => {
            let payload = null;
            try { payload = await r.json(); } catch (_) { payload = null; }
            if (!r.ok || !payload || payload.success !== true) {
                throw new Error(payload && payload.error ? String(payload.error) : ('HTTP ' + r.status));
            }
            return payload;
        })
        .then(res => {
            setTimeout(() => { location.reload(); }, 120);
        })
        .catch(error => alert("Fehler beim Löschen: " + error.message));
    };

    // --- Initialisierung am Ende nach allen Definitionen ---
    initLzSummaryCharts();
    renderChart();
    updateLzSummary();
    switchLangzeitView('day');
    setTimeout(updateLangzeitCosts, 300);
});
</script>

<!-- Statistik Editor Modal -->
<div class="modal fade" id="statsEditorModal" tabindex="-1" aria-hidden="true">
    <div class="modal-dialog modal-dialog-centered">
        <div class="modal-content shadow">
            <div class="modal-header">
                <h5 class="modal-title"><i class="fas fa-edit me-2 text-info"></i>Tageswerte anpassen</h5>
                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
            </div>
            <div class="modal-body">
                <form id="statsEditorForm">
                    <?= e3dcCsrfInput() ?>
                    <input type="hidden" name="date" id="edit-date">
                    <div class="row g-2">
                        <div class="col-6 mb-2">
                            <label class="small text-muted">PV Ertrag (kWh)</label>
                            <input type="number" step="0.01" name="pv_yield" id="edit-pv" class="form-control">
                        </div>
                        <div class="col-6 mb-2">
                            <label class="small text-muted">Hausverbrauch (kWh)</label>
                            <input type="number" step="0.01" name="home_consumption" id="edit-home" class="form-control">
                        </div>
                        <div class="col-6 mb-2">
                            <label class="small text-muted">Netzbezug (kWh)</label>
                            <input type="number" step="0.01" name="grid_in" id="edit-grid-in" class="form-control">
                        </div>
                        <div class="col-6 mb-2">
                            <label class="small text-muted">Einspeisung (kWh)</label>
                            <input type="number" step="0.01" name="grid_out" id="edit-grid-out" class="form-control">
                        </div>
                        <div class="col-6 mb-2">
                            <label class="small text-muted">Batterie Ladung (kWh)</label>
                            <input type="number" step="0.01" name="bat_in" id="edit-bat-in" class="form-control">
                        </div>
                        <div class="col-6 mb-2">
                            <label class="small text-muted">Batterie Entlad. (kWh)</label>
                            <input type="number" step="0.01" name="bat_out" id="edit-bat-out" class="form-control">
                        </div>
                        <div class="col-6">
                            <label class="small text-muted">Wallbox 1 (kWh)</label>
                            <input type="number" step="0.01" name="wb_consumption" id="edit-wb" class="form-control">
                        </div>
                        <div class="col-6">
                            <label class="small text-muted">Wallbox 2 (kWh)</label>
                            <input type="number" step="0.01" name="wb2_consumption" id="edit-wb2" class="form-control">
                        </div>
                        <div class="col-6">
                            <label class="small text-muted">Wärmepumpe (kWh)</label>
                            <input type="number" step="0.01" name="wp_consumption" id="edit-wp" class="form-control">
                        </div>
                        <div class="col-6">
                            <label class="small text-muted">Klima (kWh)</label>
                            <input type="number" step="0.01" name="climate_consumption" id="edit-climate" class="form-control">
                        </div>
                    </div>
                </form>
                <div class="alert alert-info mt-3 py-2 small">
                    <i class="fas fa-info-circle me-1"></i> Autarkie und Eigenverbrauch werden beim Speichern automatisch neu berechnet.
                </div>
            </div>
            <div class="modal-footer">
                <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Abbrechen</button>
                <button type="button" id="btn-save-stats" class="btn btn-primary px-4" onclick="saveStatsEntry()"><i class="fas fa-save me-2"></i>Speichern</button>
            </div>
        </div>
    </div>
</div>
