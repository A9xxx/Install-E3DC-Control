<?php
require_once __DIR__ . '/helpers.php';
requireWebAuth(true);
header('Content-Type: application/json');
date_default_timezone_set('Europe/Berlin');

$hours = isset($_GET['hours']) ? (float)$_GET['hours'] : 6.0;
$historyFile = '/var/www/html/ramdisk/live_history.txt';
$isArchive = false;
$wpTypeCfg = 0;
$cfgFile = '/var/www/html/data/e3dc_v4.json';
if (file_exists($cfgFile)) {
    $cfg = @json_decode(@file_get_contents($cfgFile), true);
    if (is_array($cfg)) {
        $wpTypeCfg = (int)($cfg['wp_type'] ?? 0);
    }
}

if (!empty($_GET['file']) && preg_match('/^history_\d{4}-\d{2}-\d{2}\.txt$/', $_GET['file'])) {
    $historyFile = '/var/www/html/data/history_backups/' . basename($_GET['file']);
    $isArchive = true;
}

if (!file_exists($historyFile)) {
    echo json_encode(['error' => 'No data']);
    exit;
}

$cutoff = $isArchive ? 0 : (time() - ($hours * 3600));
$lines = file($historyFile, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);

$data = [
    'labels' => [], 'pv' => [], 'home' => [], 'bat' => [], 
    'grid' => [], 'wb' => [], 'wb2' => [], 'wp' => [], 'hs' => [], 'climate' => [], 'soc' => [], 'price' => [],
    'pv_total_w' => [], 'pv_e3dc_w' => [], 'pv_external_w' => [],
    'dc0_w' => [], 'dc1_w' => [], 'grid_p1' => [], 'grid_p2' => [], 'grid_p3' => [],
    'ac_total' => [], 'wb_p1' => [], 'wb_p2' => [], 'wb_p3' => [],
    'wb2_p1' => [], 'wb2_p2' => [], 'wb2_p3' => [], 'bat_v' => [], 'bat_a' => [],
    'bat1_v' => [], 'bat1_a' => [], 'wp_vl' => [], 'wp_rl' => [], 'wp_ww' => [], 'wp_at' => [],
    'wp_kaelte' => [], 'wp_kaelte_soll' => [], 'wp_freq' => []
];

function e3dc_chart_optional_float($value) {
    if ($value === null || $value === '' || !is_numeric($value)) return null;
    return (float)$value;
}

function e3dc_chart_bool_or_null($value) {
    if ($value === null || $value === '') return null;
    if (is_bool($value)) return $value;
    $normalized = strtolower(trim((string)$value));
    if (in_array($normalized, ['1', 'true', 'yes', 'on'], true)) return true;
    if (in_array($normalized, ['0', 'false', 'no', 'off'], true)) return false;
    return null;
}

// Luxtronik-Daten vorab laden für zeitlichen Abgleich
$luxData = [];
function e3dc_chart_load_luxtronik_file($path, $cutoff, &$luxData, $tailLimit = null) {
    if (!file_exists($path)) return;
    $luxLines = @file($path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    if (!is_array($luxLines)) return;
    if ($tailLimit !== null) {
        $luxLines = array_slice($luxLines, -1 * max(1, (int)$tailLimit));
    }
    foreach ($luxLines as $ll) {
        $ld = json_decode($ll, true);
        if ($ld && isset($ld['ts'])) {
            $ts = strtotime($ld['ts']);
            if ($ts !== false && ($cutoff <= 0 || $ts >= $cutoff - 300)) {
                $luxData[$ts] = $ld['data'] ?? [];
            }
        }
    }
}

$luxArchiveDir = '/var/www/html/data/luxtronik_archive';
if ($isArchive && !empty($_GET['file']) && preg_match('/^history_(\d{4}-\d{2}-\d{2})\.txt$/', $_GET['file'], $m)) {
    e3dc_chart_load_luxtronik_file($luxArchiveDir . '/luxtronik_' . $m[1] . '.json', 0, $luxData);
} elseif (!$isArchive && $cutoff > 0) {
    $startDay = strtotime(date('Y-m-d 00:00:00', (int)max(0, $cutoff - 300)));
    $endDay = strtotime(date('Y-m-d 00:00:00', time()));
    if ($startDay !== false && $endDay !== false) {
        for ($day = $startDay; $day <= $endDay; ) {
            e3dc_chart_load_luxtronik_file($luxArchiveDir . '/luxtronik_' . date('Y-m-d', $day) . '.json', $cutoff, $luxData);
            $nextDay = strtotime('+1 day', $day);
            if ($nextDay === false || $nextDay <= $day) break;
            $day = $nextDay;
        }
    }
}

$luxFile = '/var/www/html/ramdisk/luxtronik_history.json';
e3dc_chart_load_luxtronik_file($luxFile, $cutoff, $luxData, 3000); // RAM schonen, Live-Puffer gewinnt
ksort($luxData);
$luxTimestamps = array_keys($luxData);
$luxCount = count($luxTimestamps);
$luxIndex = 0;
$lastLd = null;

$totalLines = count($lines);
$bucketSize = 900; // 15 Minuten in Sekunden (900s)

$buckets = [];

$ecoScoreFile = '/var/www/html/ramdisk/eco_score.json';
$ecoScores = [];
if (file_exists($ecoScoreFile)) {
    $loadedEcoScores = @json_decode(file_get_contents($ecoScoreFile), true);
    if (!empty($loadedEcoScores) && is_array($loadedEcoScores)) {
        $ecoScores = $loadedEcoScores;
    }
}

function e3dc_chart_price_for_ts_ms($ecoScores, $tsMs) {
    if (empty($ecoScores) || !is_array($ecoScores)) {
        return null;
    }
    foreach ($ecoScores as $score) {
        if (!is_array($score)
            || !isset($score['start_timestamp'])
            || !isset($score['end_timestamp'])
            || !isset($score['billing_price'])) {
            continue;
        }
        if ($tsMs >= $score['start_timestamp'] && $tsMs < $score['end_timestamp']) {
            return round((float)$score['billing_price'], 2);
        }
    }
    return null;
}

function e3dc_chart_normalize_price_slots(&$prices, $bucketTimestamps, $slotSeconds = 900) {
    if (empty($prices) || empty($bucketTimestamps)) {
        return;
    }
    $slots = [];
    foreach ($bucketTimestamps as $idx => $ts) {
        if (!array_key_exists($idx, $prices) || $prices[$idx] === null) {
            continue;
        }
        $slotStart = (int)(floor(((int)$ts) / $slotSeconds) * $slotSeconds);
        $key = number_format((float)$prices[$idx], 2, '.', '');
        if (!isset($slots[$slotStart])) {
            $slots[$slotStart] = ['indices' => [], 'counts' => [], 'last' => $key];
        }
        $slots[$slotStart]['indices'][] = $idx;
        $slots[$slotStart]['counts'][$key] = ($slots[$slotStart]['counts'][$key] ?? 0) + 1;
        $slots[$slotStart]['last'] = $key;
    }
    foreach ($slots as $slot) {
        if (empty($slot['indices']) || empty($slot['counts'])) {
            continue;
        }
        $selected = $slot['last'];
        $bestCount = -1;
        foreach ($slot['counts'] as $value => $count) {
            if ($count > $bestCount || ($count === $bestCount && $value === $slot['last'])) {
                $selected = $value;
                $bestCount = $count;
            }
        }
        $selectedFloat = round((float)$selected, 2);
        foreach ($slot['indices'] as $idx) {
            $prices[$idx] = $selectedFloat;
        }
    }
}

foreach ($lines as $line) {
    $d = json_decode($line, true);
    if (!$d || !isset($d['ts'])) continue;

    $ts = strtotime($d['ts']);
    if ($ts < $cutoff) continue;
    
    // Finde den passenden Luxtronik-Eintrag (nächstgelegener Zeitstempel)
    while ($luxIndex < $luxCount && $luxTimestamps[$luxIndex] <= $ts + 60) {
        $lastLd = $luxData[$luxTimestamps[$luxIndex]];
        $luxIndex++;
    }

    // Dynamisches Intervall bestimmen
    $intervalMin = 15;
    if ($hours <= 6) $intervalMin = 2;       // 6h: Alle 2 Min
    elseif ($hours <= 12) $intervalMin = 5;  // 12h: Alle 5 Min
    elseif ($hours <= 24) $intervalMin = 10; // 24h: Alle 10 Min
    // sonst 15 Minuten
    
    // Zeit auf Bucket-Größe runden
    $min = (int)date('i', $ts);
    $mRound = round($min / $intervalMin) * $intervalMin;
    $tsRounded = $ts - ($min * 60) + ($mRound * 60);
    // Sekunden nullen
    $tsRounded = $tsRounded - ($tsRounded % 60);

    if (!isset($buckets[$tsRounded])) {
        $buckets[$tsRounded] = [
            'count' => 0, 'pv' => 0, 'home_raw' => 0, 'home' => 0, 'bat' => 0, 
            'grid' => 0, 'wb' => 0, 'wb2' => 0, 'wp' => 0, 'hs' => 0, 'climate' => 0, 'soc' => 0, 'price' => 0,
            'pv_total_w' => 0, 'pv_e3dc_w' => 0, 'pv_external_w' => 0,
            'dc0_w' => 0, 'dc1_w' => 0, 'grid_p1' => 0, 'grid_p2' => 0, 'grid_p3' => 0, 'grid_p_cnt' => 0,
            'ac_total' => 0, 'wb_p1' => 0, 'wb_p2' => 0, 'wb_p3' => 0,
            'wb2_p1' => 0, 'wb2_p2' => 0, 'wb2_p3' => 0,
            'bat_v' => 0, 'bat_a' => 0, 'bat1_v' => 0, 'bat1_a' => 0,
            'wp_vl' => 0, 'wp_rl' => 0, 'wp_ww' => 0, 'wp_at' => 0, 'wp_kaelte' => 0,
            'wp_kaelte_soll' => 0, 'wp_freq' => 0,
            'p_cnt' => 0, 'b1_cnt' => 0, 'wp_cnt' => 0, 'wp_kaelte_cnt' => 0, 'wp_kaelte_soll_cnt' => 0,
            'eco_score' => 0, 'eco_cnt' => 0
        ];
    }
    
    $b = &$buckets[$tsRounded];
    $b['count']++;
    
    $pvTotal = isset($d['pv_total_w']) ? (float)$d['pv_total_w'] : (isset($d['pv']) ? (float)$d['pv'] : 0.0);
    $pvExternal = isset($d['pv_external_w']) ? max(0.0, (float)$d['pv_external_w']) : 0.0;
    $pvExternal = min($pvExternal, max(0.0, $pvTotal));
    $pvE3dc = isset($d['pv_e3dc_w']) ? max(0.0, (float)$d['pv_e3dc_w']) : max(0.0, $pvTotal - $pvExternal);

    $b['pv'] += isset($d['pv']) ? $d['pv'] : $pvTotal;
    $b['pv_total_w'] += $pvTotal;
    $b['pv_e3dc_w'] += $pvE3dc;
    $b['pv_external_w'] += $pvExternal;
    
    $homeRaw = isset($d['home_raw']) ? $d['home_raw'] : (isset($d['home']) ? $d['home'] : 0);
    $wp = isset($d['wp']) ? $d['wp'] : 0;
    $hs = isset($d['hs']) ? $d['hs'] : (isset($d['hs_power']) ? $d['hs_power'] : 0);
    $climate = isset($d['climate']) ? $d['climate'] : (isset($d['climate_power_w']) ? $d['climate_power_w'] : 0);
    $climate = max(0, is_numeric($climate) ? (float)$climate : 0);
    $wb = isset($d['wb']) ? $d['wb'] : 0;
    $wb2 = isset($d['wb2']) ? $d['wb2'] : 0;
    if ($wpTypeCfg === 2) {
        $wp = 0;
    }
    $externalWbLoad = 0;
    if (!empty($d['is_external_wb']) || (($d['wb_source'] ?? '') === 'mqtt_ha')) {
        $externalWbLoad += max(0, $wb);
    }
    if (!empty($d['is_external_wb2']) || (($d['wb2_source'] ?? '') === 'mqtt_ha')) {
        $externalWbLoad += max(0, $wb2);
    }
    $b['home'] += max(0, $homeRaw - $wp - $hs - $climate - $externalWbLoad);
    
    $b['bat'] += isset($d['bat']) ? $d['bat'] : 0;
    $b['grid'] += isset($d['grid']) ? $d['grid'] : 0;
    $b['wb'] += $wb;
    $b['wb2'] += $wb2;
    $b['wp'] += $wp;
    $b['hs'] += $hs;
    $b['climate'] += $climate;
    $b['soc'] += isset($d['soc']) ? $d['soc'] : 0;
    
    if (isset($d['price_ct'])) { $b['price'] += $d['price_ct']; $b['p_cnt']++; }
    if (isset($d['optimization_score'])) { $b['eco_score'] += $d['optimization_score']; $b['eco_cnt']++; }
    
    $b['dc0_w'] += isset($d['dc0_w']) ? $d['dc0_w'] : 0;
    $b['dc1_w'] += isset($d['dc1_w']) ? $d['dc1_w'] : 0;
    $gridPhaseValues = [
        e3dc_chart_optional_float($d['grid_p1'] ?? null),
        e3dc_chart_optional_float($d['grid_p2'] ?? null),
        e3dc_chart_optional_float($d['grid_p3'] ?? null),
    ];
    $gridPhaseComplete = $gridPhaseValues[0] !== null && $gridPhaseValues[1] !== null && $gridPhaseValues[2] !== null;
    $gridPmAvailable = e3dc_chart_bool_or_null($d['grid_pm_available'] ?? null);
    $gridPhaseHasSignal = $gridPhaseComplete && (abs($gridPhaseValues[0]) + abs($gridPhaseValues[1]) + abs($gridPhaseValues[2]) > 0.1);
    $gridPhaseUsable = $gridPhaseComplete && (($gridPmAvailable === true) || ($gridPmAvailable === null && $gridPhaseHasSignal));
    if ($gridPhaseUsable) {
        $b['grid_p1'] += $gridPhaseValues[0];
        $b['grid_p2'] += $gridPhaseValues[1];
        $b['grid_p3'] += $gridPhaseValues[2];
        $b['grid_p_cnt']++;
    }
    $b['ac_total'] += (isset($d['ac0_w']) ? $d['ac0_w'] : 0) + (isset($d['ac1_w']) ? $d['ac1_w'] : 0) + (isset($d['ac2_w']) ? $d['ac2_w'] : 0);
    $b['wb_p1'] += isset($d['wb_p1']) ? $d['wb_p1'] : 0;
    $b['wb_p2'] += isset($d['wb_p2']) ? $d['wb_p2'] : 0;
    $b['wb_p3'] += isset($d['wb_p3']) ? $d['wb_p3'] : 0;
    $b['wb2_p1'] += isset($d['wb2_p1']) ? $d['wb2_p1'] : 0;
    $b['wb2_p2'] += isset($d['wb2_p2']) ? $d['wb2_p2'] : 0;
    $b['wb2_p3'] += isset($d['wb2_p3']) ? $d['wb2_p3'] : 0;
    $b['bat_v'] += isset($d['bat_v']) ? $d['bat_v'] : 0;
    $b['bat_a'] += isset($d['bat_a']) ? $d['bat_a'] : 0;
    
    if (!empty($d['bat1_v'])) { $b['bat1_v'] += $d['bat1_v']; $b['bat1_a'] += $d['bat1_a']; $b['b1_cnt']++; }

    $khl = null;
    foreach (['wp_kaelte_temp', 'Kaeltespeicher_Ist', 'Kaeltespeicher_Temp', 'Kältespeicher_Ist'] as $khlKey) {
        if (isset($d[$khlKey]) && is_numeric($d[$khlKey])) {
            $khl = (float)$d[$khlKey];
            break;
        }
    }
    $khlSoll = null;
    foreach (['wp_kaelte_soll', 'Kaeltespeicher_Soll', 'Kältespeicher_Soll'] as $khlSollKey) {
        if (isset($d[$khlSollKey]) && is_numeric($d[$khlSollKey])) {
            $khlSoll = (float)$d[$khlSollKey];
            break;
        }
    }
    
    if (!empty($lastLd)) {
        $vl = isset($lastLd['Vorlauf_Ist']) ? $lastLd['Vorlauf_Ist'] : (isset($lastLd['Vorlauf']) ? $lastLd['Vorlauf'] : null);
        $rl = isset($lastLd['Ruecklauf_Ist']) ? $lastLd['Ruecklauf_Ist'] : (isset($lastLd['Rücklauf']) ? $lastLd['Rücklauf'] : null);
        $ww = isset($lastLd['Warmwasser_Ist']) ? $lastLd['Warmwasser_Ist'] : (isset($lastLd['Warmwasser-Ist']) ? $lastLd['Warmwasser-Ist'] : null);
        $at = isset($lastLd['Aussentemp']) ? $lastLd['Aussentemp'] : (isset($lastLd['Außentemperatur']) ? $lastLd['Außentemperatur'] : null);
        
        if ($vl !== null || $rl !== null || $ww !== null || $at !== null) {
            $b['wp_vl'] += $vl ?: 0;
            $b['wp_rl'] += $rl ?: 0;
            $b['wp_ww'] += $ww ?: 0;
            $b['wp_at'] += $at ?: 0;
            $b['wp_freq'] += isset($lastLd['Freq_Ist']) ? $lastLd['Freq_Ist'] : 0;
            $b['wp_cnt']++;
        }

        if ($khl === null) {
            foreach (['Kaeltespeicher_Ist', 'Kaeltespeicher_Temp', 'Kältespeicher_Ist'] as $khlKey) {
                if (isset($lastLd[$khlKey]) && is_numeric($lastLd[$khlKey])) {
                    $khl = (float)$lastLd[$khlKey];
                    break;
                }
            }
        }
        if ($khlSoll === null) {
            foreach (['Kaeltespeicher_Soll', 'Kältespeicher_Soll'] as $khlSollKey) {
                if (isset($lastLd[$khlSollKey]) && is_numeric($lastLd[$khlSollKey])) {
                    $khlSoll = (float)$lastLd[$khlSollKey];
                    break;
                }
            }
        }
    }

    if ($khl !== null) {
        $b['wp_kaelte'] += $khl;
        $b['wp_kaelte_cnt']++;
    }
    if ($khlSoll !== null) {
        $b['wp_kaelte_soll'] += $khlSoll;
        $b['wp_kaelte_soll_cnt']++;
    }
}

ksort($buckets);

foreach ($buckets as $ts => $b) {
    $c = $b['count'];
    $data['labels'][] = date('H:i', $ts);
    $data['pv'][] = round($b['pv'] / $c);
    $data['pv_total_w'][] = round($b['pv_total_w'] / $c);
    $data['pv_e3dc_w'][] = round($b['pv_e3dc_w'] / $c);
    $data['pv_external_w'][] = round($b['pv_external_w'] / $c);
    $data['home'][] = round($b['home'] / $c);
    $data['bat'][] = round($b['bat'] / $c);
    $data['grid'][] = round($b['grid'] / $c);
    $data['wb'][] = round($b['wb'] / $c);
    $data['wb2'][] = round($b['wb2'] / $c);
    $data['wp'][] = round($b['wp'] / $c);
    $data['hs'][] = round($b['hs'] / $c);
    $data['climate'][] = round($b['climate'] / $c);
    $data['soc'][] = round($b['soc'] / $c, 1);
    
    $slotPrice = e3dc_chart_price_for_ts_ms($ecoScores, $ts * 1000);
    $data['price'][] = $slotPrice !== null ? $slotPrice : ($b['p_cnt'] > 0 ? round($b['price'] / $b['p_cnt'], 2) : null);
    $data['eco_score'][] = $b['eco_cnt'] > 0 ? round($b['eco_score'] / $b['eco_cnt']) : null;
    
    $data['dc0_w'][] = round($b['dc0_w'] / $c);
    $data['dc1_w'][] = round($b['dc1_w'] / $c);
    $data['grid_p1'][] = $b['grid_p_cnt'] > 0 ? round($b['grid_p1'] / $b['grid_p_cnt']) : null;
    $data['grid_p2'][] = $b['grid_p_cnt'] > 0 ? round($b['grid_p2'] / $b['grid_p_cnt']) : null;
    $data['grid_p3'][] = $b['grid_p_cnt'] > 0 ? round($b['grid_p3'] / $b['grid_p_cnt']) : null;
    $data['ac_total'][] = round($b['ac_total'] / $c);
    $data['wb_p1'][] = round($b['wb_p1'] / $c);
    $data['wb_p2'][] = round($b['wb_p2'] / $c);
    $data['wb_p3'][] = round($b['wb_p3'] / $c);
    $data['wb2_p1'][] = round($b['wb2_p1'] / $c);
    $data['wb2_p2'][] = round($b['wb2_p2'] / $c);
    $data['wb2_p3'][] = round($b['wb2_p3'] / $c);
    $data['bat_v'][] = round($b['bat_v'] / $c, 1);
    $data['bat_a'][] = round($b['bat_a'] / $c, 1);
    
    $data['bat1_v'][] = $b['b1_cnt'] > 0 ? round($b['bat1_v'] / $b['b1_cnt'], 1) : null;
    $data['bat1_a'][] = $b['b1_cnt'] > 0 ? round($b['bat1_a'] / $b['b1_cnt'], 1) : null;
    
    $data['wp_vl'][] = $b['wp_cnt'] > 0 ? round($b['wp_vl'] / $b['wp_cnt'], 1) : null;
    $data['wp_rl'][] = $b['wp_cnt'] > 0 ? round($b['wp_rl'] / $b['wp_cnt'], 1) : null;
    $data['wp_ww'][] = $b['wp_cnt'] > 0 ? round($b['wp_ww'] / $b['wp_cnt'], 1) : null;
    $data['wp_at'][] = $b['wp_cnt'] > 0 ? round($b['wp_at'] / $b['wp_cnt'], 1) : null;
    $data['wp_kaelte'][] = $b['wp_kaelte_cnt'] > 0 ? round($b['wp_kaelte'] / $b['wp_kaelte_cnt'], 1) : null;
    $data['wp_kaelte_soll'][] = $b['wp_kaelte_soll_cnt'] > 0 ? round($b['wp_kaelte_soll'] / $b['wp_kaelte_soll_cnt'], 1) : null;
    $data['wp_freq'][] = $b['wp_cnt'] > 0 ? round($b['wp_freq'] / $b['wp_cnt'], 1) : null;
}

$bucketKeys = array_keys($buckets);
e3dc_chart_normalize_price_slots($data['price'], $bucketKeys, $bucketSize);

// Reale V4 Eco-Scores einlesen (deckt auch die Vergangenheit des heutigen Tages ab!)
// Historische Eco-Scores kommen nun direkt aus der live_history.txt,
// eco_score.json füllt nur Lücken auf (z.B. Zukunft oder vor dem Update)
if (!empty($ecoScores) && is_array($ecoScores)) {
    for ($i = 0; $i < count($bucketKeys); $i++) {
        $tsMs = $bucketKeys[$i] * 1000;
        foreach ($ecoScores as $score) {
            if ($tsMs >= $score['start_timestamp'] && $tsMs < $score['end_timestamp']) {
                if ($data['eco_score'][$i] === null) {
                    $data['eco_score'][$i] = round($score['optimization_score']);
                }
                break;
            }
        }
    }
}

echo json_encode($data);
