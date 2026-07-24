<?php
/**
 * logic.php - Zentrale Datenaufbereitung für Mobile & Desktop Dashboard
 * Liest Konfiguration, History-Mittelwerte und aWATTar-Prognosen.
 * Setzt voraus, dass helpers.php bereits eingebunden wurde.
 */

$paths = getInstallPaths();
// Konfigurations-Pfad wird nicht mehr direkt gelesen — loadE3dcConfig() nutzt V4 JSON
$historyFile = rtrim($paths['install_path'], '/') . '/live_history.txt';

// 1. Config aus V4 JSON (Single Source of Truth via loadE3dcConfig)
$wpMax = 5000;
$pvMax = 10000; // Fallback
$maxBatPower = 3000; // Fallback
$batteryCapacity = 0; // Fallback
$gridMaxAmps = 63; // Fallback (Ampere pro Phase)
$lat = 51.16; $lon = 10.45; // Default (Mitte DE)
$pvStrings = [];
$showForecast = true; // Default an
$darkMode = true; // Default an
$frontendVariant = 'classic';
$frontendDetailMode = 'normal';
$pvAtmosphere = 0.7; // Default atmosph rische Transmission
$luxtronikEnabled = false; // Default aus
$luxtronikIp = '0.0.0.0'; // Nicht konfiguriert; eine echte Adresse muss aus der Konfiguration kommen
$wpEnabled = false; // Standardmaessig aus, analog zum C++ Kern
$wpType = 0; // 0=Luxtronik, 1=IDM, 2=Shelly/Other
$wbEnabled = true; // Standardmaessig an, falls nicht in config
$hsEnabled = false; // Standardmaessig aus, bis IP gefunden wird
$showPriceTrend = false; // Standardmaessig aus

$_v4conf = loadE3dcConfig();
$_c = $_v4conf['config'] ?? [];

if (!empty($_c['wpmax'])) { $wpMax = parseConfigFloat($_c['wpmax']) * 1000; }
if (!empty($_c['maximumladeleistung'])) { $maxBatPower = parseConfigFloat($_c['maximumladeleistung']); }
if (!empty($_c['speichergroesse'])) { $batteryCapacity = parseConfigFloat($_c['speichergroesse']); }
if (!empty($_c['grid_max_amps'])) { $gridMaxAmps = parseConfigFloat($_c['grid_max_amps']); }
if (!empty($_c['hoehe'])) { $lat = parseConfigFloat($_c['hoehe']); }
if (!empty($_c['laenge'])) { $lon = parseConfigFloat($_c['laenge']); }
if (isset($_c['show_forecast'])) {
    $v = strtolower((string)$_c['show_forecast']);
    $showForecast = ($v === '1' || $v === 'true');
}
if (isset($_c['darkmode'])) {
    $v = strtolower((string)$_c['darkmode']);
    $darkMode = ($v === '1' || $v === 'true');
}
$frontendVariantRaw = strtolower(trim((string)($_c['frontend_variant'] ?? 'classic')));
$frontendVariant = in_array($frontendVariantRaw, ['classic', 'modern'], true) ? $frontendVariantRaw : 'classic';
$frontendDetailModeRaw = strtolower(trim((string)($_c['frontend_detail_mode'] ?? 'normal')));
$frontendDetailMode = in_array($frontendDetailModeRaw, ['compact', 'normal', 'detail'], true) ? $frontendDetailModeRaw : 'normal';
if (!empty($_c['pvatmosphere'])) { $pvAtmosphere = parseConfigFloat($_c['pvatmosphere']); }
if (isset($_c['luxtronik'])) {
    $v = strtolower((string)$_c['luxtronik']);
    $luxtronikEnabled = ($v === '1' || $v === 'true');
}
if (!empty($_c['luxtronik_ip'])) { $luxtronikIp = $_c['luxtronik_ip']; }
$wpType = getHeatpumpTypeConfig($_c);
$wpEnabled = isHeatpumpEnabledConfig($_c);
$hsEnabled = isHeaterEnabledConfig($_c);

$awattar = (int)($_c['awattar'] ?? 0);
$showPriceTrend = ($awattar === 1);
if (!$showPriceTrend) {
    // Fallback: e3dc.strompreise.txt (Legacy) prüfen. Die optionale Datei darf
    // bei fehlender Leseberechtigung oder einem Race niemals das Frontend
    // durch count(false) unbenutzbar machen.
    $strompreiseFile = rtrim($paths['install_path'], '/') . '/e3dc.strompreise.txt';
    if (is_file($strompreiseFile) && is_readable($strompreiseFile)) {
        $lines = @file($strompreiseFile, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
        if (is_array($lines) && count($lines) > 1) { $showPriceTrend = true; }
    }
}

// Wallbox-Aktivierung aus V4 Config. Ein explizites "none" blendet die UI-Kacheln aus.
$wbEnabled = hasAnyWallboxConfig($_c);

if ($wpType === 2) {
    $hsEnabled = true;
    $wpEnabled = false;
}
if (!empty($_c['heizstab_ip']) && $_c['heizstab_ip'] !== '0.0.0.0') { $hsEnabled = true; }
if (!empty($_c['shelly_heiz_ip']) && $_c['shelly_heiz_ip'] !== '0.0.0.0') { $hsEnabled = true; }

// PV-Strings aus Config (forecast1..N)
for ($i = 1; $i <= 5; $i++) {
    $fKey = 'forecast' . $i;
    if (!empty($_c[$fKey]) && preg_match('/([\d\.\-]+)\/([\d\.\-]+)\/([\d\.]+)/', $_c[$fKey], $m)) {
        $p = parseConfigFloat($m[3]) * 1000;
        $pvStrings[] = ['tilt' => parseConfigFloat($m[1]), 'azimuth' => parseConfigFloat($m[2]), 'power' => $p];
        $pvMax  = isset($pvMax) ? $pvMax + $p : $p; // Addieren falls mehrere
    }
}
if (!empty($pvStrings)) { $pvMax = array_sum(array_column($pvStrings, 'power')); }


// 2. 24h Mittelwerte berechnen (Cache 1 Std)
$avgs = get24hAverages($historyFile);

// 3. Strompreise & Prognose aus awattardebug.txt laden
$priceHistory = [];
$forecastData = [];
$priceStartHour = 0;
$priceInterval = 1.0;
$currentHour = (int)date('H');

// Intelligente Dateiauswahl & Merge (0.txt/12.txt + live debug.txt)
$baseFile = 'awattardebug.0.txt';
if ($currentHour >= 22 || $currentHour < 10) {
    $baseFile = 'awattardebug.12.txt';
}

$filesToRead = [];
$fBase = rtrim($paths['install_path'], '/') . '/' . $baseFile;
if (file_exists($fBase)) $filesToRead[] = $fBase;
elseif ($baseFile === 'awattardebug.12.txt') {
    $f0 = rtrim($paths['install_path'], '/') . '/awattardebug.0.txt';
    if (file_exists($f0)) $filesToRead[] = $f0;
}
$fLive = rtrim($paths['install_path'], '/') . '/awattardebug.txt';
if (file_exists($fLive)) $filesToRead[] = $fLive;

$chartDataMap = [];
$forecastDataMap = [];

// --- NEU: RAM-Disk Caching für Basis-Prognosedaten ---
$logicCacheFile = '/var/www/html/ramdisk/logic_awattar_cache.json';
$mtimeSum = 0;
foreach ($filesToRead as $f) { if (file_exists($f)) $mtimeSum += filemtime($f); }

$useLogicCache = false;
if (file_exists($logicCacheFile)) {
    $cData = @json_decode(file_get_contents($logicCacheFile), true);
    if ($cData && isset($cData['mtime']) && $cData['mtime'] === $mtimeSum) {
        $priceHistory = $cData['priceHistory'];
        $forecastData = $cData['forecastData'];
        $priceStartHour = $cData['priceStartHour'];
        $priceInterval = $cData['priceInterval'];
        $useLogicCache = true;
    }
}

if (!$useLogicCache) {
    foreach ($filesToRead as $file) {
        $readingData = false; $lastTime = -1; $dayOffset = 0;
        foreach (file($file, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) as $line) {
            $trimmed = trim($line);
            if ($trimmed === 'Data') { $readingData = true; $lastTime = -1; $dayOffset = 0; continue; }
            if ($trimmed === 'DV' || $trimmed === 'Simulation') { $readingData = false; $lastTime = -1; $dayOffset = 0; continue; }
            
            $cols = preg_split('/\s+/', $trimmed);
            if (count($cols) < 1 || !is_numeric($cols[0])) continue;

            $rawTime = (float)$cols[0];
            if ($lastTime !== -1 && $rawTime < $lastTime) { $dayOffset += 24; }
            $lastTime = $rawTime;
            $h = number_format($rawTime + $dayOffset, 2, '.', '');

            if (!$readingData) {
                if (count($cols) >= 2 && is_numeric($cols[1])) $chartDataMap[$h] = (float)$cols[1];
            } else {
                if (count($cols) >= 5 && is_numeric($cols[4])) $forecastDataMap[$h] = (float)$cols[4] * $batteryCapacity * 40;
            }
        }
    }

    uksort($chartDataMap, function($a, $b) { return (float)$a <=> (float)$b; });
    uksort($forecastDataMap, function($a, $b) { return (float)$a <=> (float)$b; });

    foreach ($chartDataMap as $h => $val) { if (empty($priceHistory)) $priceStartHour = (float)$h; elseif (count($priceHistory) === 1) $priceInterval = max(0.25, (float)$h - $priceStartHour); $priceHistory[] = $val; }
    foreach ($forecastDataMap as $h => $val) { $forecastData[] = ['h' => (float)$h, 'w' => $val]; }
    
    @file_put_contents($logicCacheFile, json_encode([
        'mtime' => $mtimeSum,
        'priceHistory' => $priceHistory,
        'forecastData' => $forecastData,
        'priceStartHour' => $priceStartHour,
        'priceInterval' => $priceInterval
    ]));
    @chmod($logicCacheFile, 0664);
}

// Stabile Tages-PV-Prognose aus V4 ML oder awattardebug.23.txt
$stablePvForecastKwh = 0.0;
// 1. NEU: Versuch, die ML Prognose einzulesen
$mlPredFile = '/var/www/html/ramdisk/ml_prediction.json';
if (file_exists($mlPredFile)) {
    $mlPredData = @json_decode(file_get_contents($mlPredFile), true);
    if (is_array($mlPredData) && isset($mlPredData['pv_kwh'])) {
        $stablePvForecastKwh = (float)$mlPredData['pv_kwh'];
    }
}

// 2. FALLBACK: Alte Eba awattardebug.23.txt lesen
if ($stablePvForecastKwh <= 0.0) {
    $f23 = rtrim($paths['install_path'], '/') . '/awattardebug.23.txt';
    if (file_exists($f23)) {
        $inData23 = false;
        $last23 = -1;
        $off23 = 0;
        foreach (@file($f23, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) as $l23) {
            $l23 = trim($l23);
            if ($l23 === 'Data') { $inData23 = true; $last23 = -1; $off23 = 0; continue; }
            if ($l23 === 'Simulation' || $l23 === 'DV') { $inData23 = false; continue; }
            if (!$inData23) continue;
            $c23 = preg_split('/\s+/', $l23);
            if (count($c23) < 5 || !is_numeric($c23[0]) || !is_numeric($c23[4])) continue;
            $t23 = (float)$c23[0];
            if ($last23 !== -1 && $t23 < $last23) $off23 += 24;
            $last23 = $t23;
            $tAbs23 = $t23 + $off23;
            // Die Datei beginnt um 23.00 (tAbs = 23).
            // Um 0.00 springt tAbs auf 24.00. Der komplette nächste Tag (die echte "heute" Prognose) 
            // liegt also im Bereich tAbs23 >= 24.0 und tAbs23 < 48.0.
            if ($tAbs23 >= 24 && $tAbs23 < 48) {
                // Slot-Intervall aus Zeitdifferenz bestimmen (typisch 0.25h = 15 Min)
                $stablePvForecastKwh += ((float)$c23[4] * $batteryCapacity * 40.0 / 1000.0) * 0.25;
            }
        }
    }
    $stablePvForecastKwh = round($stablePvForecastKwh, 1);
}

// Fallback: wenn .23.txt fehlt, aus FORECAST_DATA summieren (alle h=0..24)
if ($stablePvForecastKwh <= 0) {
    foreach ($forecastData as $entry) {
        if ($entry['h'] >= 0 && $entry['h'] < 24) {
            $stablePvForecastKwh += ($entry['w'] / 1000.0) * 0.25;
        }
    }
    $stablePvForecastKwh = round($stablePvForecastKwh, 1);
}

// --- NEU: V4 PV Ensemble (Überschreibt C++ Logik komplett, falls aktiv) ---
$pvForecastFile = '/var/www/html/ramdisk/pv_forecast.json';
if (file_exists($pvForecastFile)) {
    $pvForecastsParsed = @json_decode(file_get_contents($pvForecastFile), true);
    if ($pvForecastsParsed && is_array($pvForecastsParsed)) {
        $forecastData = [];
        $stablePvForecastKwh = 0.0;
        $remainingPvForecastKwh = 0.0;
        $actualPvTodayKwh = 0.0;
        $midnightMs = strtotime('today') * 1000;
        $nowMs = time() * 1000;
        $dailyStatsFile = '/var/www/html/ramdisk/daily_stats.json';
        if (file_exists($dailyStatsFile)) {
            $dailyStats = @json_decode(file_get_contents($dailyStatsFile), true);
            if (is_array($dailyStats) && isset($dailyStats['pv_today_kwh']) && is_numeric($dailyStats['pv_today_kwh'])) {
                $actualPvTodayKwh = max(0.0, (float)$dailyStats['pv_today_kwh']);
            }
        }
        if ($actualPvTodayKwh <= 0.0) {
            $liveFile = '/var/www/html/ramdisk/live_data_py.json';
            if (file_exists($liveFile)) {
                $liveData = @json_decode(file_get_contents($liveFile), true);
                if (is_array($liveData) && isset($liveData['PV_Energy_kWh']) && is_numeric($liveData['PV_Energy_kWh'])) {
                    $actualPvTodayKwh = max(0.0, (float)$liveData['PV_Energy_kWh']);
                }
            }
        }
        
        foreach ($pvForecastsParsed as $pf) {
            $tsMs = $pf['start_timestamp'];
            // Nur Daten für "Heute" betrachten (0-24h)
            if ($tsMs >= $midnightMs && $tsMs < $midnightMs + (24 * 3600 * 1000)) {
                $h = ($tsMs - $midnightMs) / (3600 * 1000);
                
                // solar.js erwartet Watt ('w') in entry.w und multipliziert selbst mit 0.25h
                // predicted_kwh liefert laut get_forecast_data.php tatsächlich kW! (wird dort mit 0.25 multipliziert)
                // W = kW * 1000
                $energy_w = (float)$pf['predicted_kwh'] * 1000;
                
                $forecastData[] = ['h' => (float)$h, 'w' => $energy_w];
                $slotKwh = ((float)$pf['predicted_kwh'] * 0.25);
                $stablePvForecastKwh += $slotKwh;
                if ($tsMs >= $nowMs) {
                    $remainingPvForecastKwh += $slotKwh;
                }
            }
        }
        // Erwarteter Tagesertrag: volle Prognose, mindestens aber Ist-Ertrag plus Restprognose.
        // So bleibt die Kachel plausibel, wenn die V4-Prognose erst nach Tagesbeginn nur Restslots enthaelt.
        $stablePvForecastKwh = round(max($stablePvForecastKwh, $actualPvTodayKwh + $remainingPvForecastKwh), 1);
    }
}

// --- NEU: V4 Strompreise (Überschreibt C++ Logik komplett, falls aktiv) ---
$ecoScoreFile = '/var/www/html/ramdisk/eco_score.json';
if (file_exists($ecoScoreFile)) {
    $ecoScoresParsed = @json_decode(file_get_contents($ecoScoreFile), true);
    if ($ecoScoresParsed && is_array($ecoScoresParsed)) {
        $priceHistory = [];
        $midnightMs = strtotime('today') * 1000;
        $activeTariffStartMs = -1;
        
        foreach ($ecoScoresParsed as $score) {
            $tsMs = $score['start_timestamp'];
            // Sammle Preise für Heute (0-24h) für die Trend-Linie
            if ($tsMs >= $midnightMs && $tsMs < $midnightMs + (24 * 3600 * 1000)) {
                if ($activeTariffStartMs === -1) {
                    $activeTariffStartMs = $tsMs;
                }
                $priceHistory[] = (float)$score['billing_price'];
            }
        }
        
        if ($activeTariffStartMs !== -1) {
            $h = ($activeTariffStartMs - $midnightMs) / (3600 * 1000);
            $priceStartHour = (int)floor($h);
        } else {
            $priceStartHour = 0;
        }
        // Da V4 dynamisch in 15-Min oder 1-h blockiert, das Intervall aus der Länge ableiten
        $priceInterval = count($priceHistory) > 24 ? 0.25 : 1.0;
    }
}
?>

