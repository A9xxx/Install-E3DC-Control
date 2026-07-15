<?php

if (!function_exists('getInstallPaths')) {
    require_once __DIR__ . '/helpers.php';
}
requireWebAuth(false);

// Wenn vital_stats.py noch nie ausgeführt wurde
$paths = getInstallPaths();
$installPath = $paths['install_path'];
$python = getPythonInterpreter();
$vitalsScript = null;
$candidateScripts = [
    rtrim($installPath, '/') . '/Installer/vital_stats.py',
    rtrim(dirname($installPath), '/') . '/Install/Installer/vital_stats.py',
    '/app/pi/Install/Installer/vital_stats.py',
    '/home/pi/Install/Installer/vital_stats.py',
];
foreach ($candidateScripts as $candidateScript) {
    if (file_exists($candidateScript)) {
        $vitalsScript = $candidateScript;
        break;
    }
}
// Führe das Skript bei Abruf einmal synchron aus (dauert 1-2 Sek via RSCP)
// Wir cachen hier nichts, um Livedaten-Drift perfekt zu überwachen, wenn der User den Tab öffnet
$output = $vitalsScript
    ? shell_exec(escapeshellarg($python) . " " . escapeshellarg($vitalsScript) . " --once 2>&1")
    : null;
$vitalsError = trim((string)($output ?? ''));
if (!$vitalsScript) {
    $vitalsError = 'vital_stats.py wurde im Installationspfad nicht gefunden.';
    $output = $vitalsError;
}

$vitals = null;
if ($output) {
    $startPos = strpos($output, '{');
    if ($startPos !== false) {
        $jsonOutput = substr($output, $startPos);
        $vitals = json_decode($jsonOutput, true);
    }
}

function vitalRiskClass($level) {
    return [
        'ok' => ['success', 'Unauffällig'],
        'watch' => ['info', 'Beobachten'],
        'warn' => ['warning', 'Auffällig'],
        'critical' => ['danger', 'Kritisch'],
    ][$level] ?? ['secondary', 'Unbekannt'];
}

function sohRiskLevel($soh) {
    if ($soh === null) return 'watch';
    if ($soh < 80) return 'critical';
    if ($soh < 85) return 'warn';
    if ($soh < 90) return 'watch';
    return 'ok';
}

function driftRiskLevel($spreadV) {
    if ($spreadV === null) return 'watch';
    if ($spreadV > 0.100) return 'critical';
    if ($spreadV > 0.050) return 'warn';
    if ($spreadV > 0.030) return 'watch';
    return 'ok';
}

function fmtVital($value, $dec = 1, $suffix = '') {
    if ($value === null || $value === '') return 'N/A';
    return number_format((float)$value, $dec, ',', '.') . $suffix;
}

function fmtYears($value) {
    if ($value === null || $value === '') return 'nicht belastbar';
    return number_format((float)$value, 1, ',', '.') . ' Jahre';
}

function prognosisDateFromYears($years, $baseTs = null) {
    if ($years === null || $years === '') return null;
    $yearsFloat = (float)$years;
    if (!is_finite($yearsFloat)) return null;
    $days = max(0, (int)round($yearsFloat * 365.2425));
    try {
        $base = new DateTimeImmutable('@' . (int)($baseTs ?? time()));
        $base = $base->setTimezone(new DateTimeZone(date_default_timezone_get()));
        return $base->modify('+' . $days . ' days');
    } catch (Exception $e) {
        return null;
    }
}

function fmtPrognosisYear($years, $baseTs = null) {
    $date = prognosisDateFromYears($years, $baseTs);
    return $date ? $date->format('Y') : 'offen';
}

function fmtPrognosisMonthYear($years, $baseTs = null) {
    $date = prognosisDateFromYears($years, $baseTs);
    return $date ? $date->format('m/Y') : 'offen';
}

function prognosisClass($value, $warnYears, $dangerYears = null) {
    if ($value === null || $value === '') return 'text-muted';
    if ($dangerYears !== null && $value < $dangerYears) return 'text-danger';
    return $value < $warnYears ? 'text-warning' : 'text-success';
}

// --- DEGRADATION & KAPAZITÄTS-PROGNOSE ---
$brutto_installiert = 0.0;
$speicher_gross = 0.0;

if (!empty($vitals['system_info']['usable_capacity_wh'])) {
    // FCC (Full Charge Capacity) in Ah * 51.8V = Wh (entspricht der installierten Brutto-Hardware)
    $brutto_installiert = floatval($vitals['system_info']['usable_capacity_wh']) / 1000.0;
    
    // Echte nutzbare Kapazitaet im Neuzustand (USABLE_CAPACITY, ca. 10% weniger als Brutto)
    if (!empty($vitals['system_info']['real_usable_capacity_wh'])) {
        $speicher_gross = floatval($vitals['system_info']['real_usable_capacity_wh']) / 1000.0;
    } else {
        $speicher_gross = $brutto_installiert * 0.9;
    }
    $netto_basis = $speicher_gross;
} elseif (!empty($vitals['system_info']['installed_capacity_wh'])) {
    // Fallback: BAT_SPECIFIED_CAPACITY (Datenblatt-Nennkapazitaet, meist hoeher als real)
    $brutto_installiert = floatval($vitals['system_info']['installed_capacity_wh']) / 1000.0;
    // E3DC reserviert hardwareseitig ca. 10% (Usable = 90% von Design Capacity)
    $speicher_gross = $brutto_installiert * 0.9;
    $netto_basis = $speicher_gross;
} else {
    // Letzter Fallback: speichergroesse aus V4 Config
    $_vconf = loadE3dcConfig();
    $_vsize = parseConfigFloat($_vconf['config']['speichergroesse'] ?? '0');
    if ($_vsize > 0) {
        $speicher_gross = $_vsize;
        $brutto_installiert = $speicher_gross / 0.9;
        $netto_basis = $speicher_gross;
    }
}


$systemAgeYears = 0;
$prodString = $vitals['system_info']['production_date'] ?? '';
if (preg_match('/KW(\d+)\s+(\d{4})/', $prodString, $matches)) {
    $kw = $matches[1];
    $year = $matches[2];
    $date = new DateTime();
    $date->setISODate($year, $kw);
    $now = new DateTime();
    $systemAgeYears = max(0.1, $date->diff($now)->days / 365.25);
}

$totalPacks = 0;
$weightedSohSum = 0;
$maxCycles = 0;
$capacityWeightedSohSum = 0;
$capacityWeightKwh = 0;
$minSoh = null;
$maxSoh = null;
$worstPack = null;
$maxVoltageSpread = null;
$maxTempSpread = null;
$diagnosticHints = [];

if ($vitals && !empty($vitals['cabinets'])) {
    foreach ($vitals['cabinets'] as $cab) {
       $cabCapacityKwh = ((float)($cab['usable_wh'] ?? $cab['specified_wh'] ?? 0)) / 1000.0;
       if ($cabCapacityKwh > 0 && isset($cab['soh_avg']) && $cab['soh_avg'] !== null) {
           $capacityWeightedSohSum += $cabCapacityKwh * (float)$cab['soh_avg'];
           $capacityWeightKwh += $cabCapacityKwh;
       }
       foreach($cab['packs'] as $pack) {
           if (!isset($pack['soh'])) continue;
           $totalPacks++;
           $packSoh = (float)$pack['soh'];
           $weightedSohSum += $packSoh;
           $packCycles = (int)($pack['cycles'] ?? 0);
           $maxCycles = max($maxCycles, $packCycles);
           if ($minSoh === null || $packSoh < $minSoh) {
               $minSoh = $packSoh;
               $worstPack = [
                   'cabinet' => $cab['index'],
                   'pack' => $pack['index'],
                   'soh' => $packSoh,
                   'cycles' => $packCycles,
               ];
           }
           $maxSoh = $maxSoh === null ? $packSoh : max($maxSoh, $packSoh);
           if (isset($pack['voltage_spread']) && $pack['voltage_spread'] !== null) {
               $maxVoltageSpread = $maxVoltageSpread === null ? (float)$pack['voltage_spread'] : max($maxVoltageSpread, (float)$pack['voltage_spread']);
           }
           if (isset($pack['temp_spread']) && $pack['temp_spread'] !== null) {
               $maxTempSpread = $maxTempSpread === null ? (float)$pack['temp_spread'] : max($maxTempSpread, (float)$pack['temp_spread']);
           }
       }
    }
}

$avgSoh = $capacityWeightKwh > 0
    ? $capacityWeightedSohSum / $capacityWeightKwh
    : ($totalPacks > 0 ? $weightedSohSum / $totalPacks : null);
$avgSohMethod = $capacityWeightKwh > 0 ? 'kapazitätsgewichtet' : 'Pack-Durchschnitt';
$sohSpread = ($minSoh !== null && $maxSoh !== null) ? $maxSoh - $minSoh : null;
if ($avgSoh === null) {
    $avgSohMethod = 'nicht belastbar';
}
$overallLevel = sohRiskLevel($minSoh);
if (driftRiskLevel($maxVoltageSpread) === 'critical') $overallLevel = 'critical';
elseif ($overallLevel !== 'critical' && driftRiskLevel($maxVoltageSpread) === 'warn') $overallLevel = 'warn';
[$overallColor, $overallText] = vitalRiskClass($overallLevel);

if ($minSoh !== null && $minSoh < 80) {
    $diagnosticHints[] = "Ein Batterie-Pack liegt unter 80% SOH. Das ist ein belastbarer Anlass, die Werte zu dokumentieren und beim Support anzufragen.";
} elseif ($minSoh !== null && $minSoh < 85) {
    $diagnosticHints[] = "Ein Batterie-Pack nähert sich der 80%-Schwelle. Verlauf beobachten und Screenshots/Exports sichern.";
}
if ($maxVoltageSpread !== null && $maxVoltageSpread > 0.050) {
    $diagnosticHints[] = "Die Zellspannungsdrift ist erhöht. Das kann Balancing, Temperatur oder beginnende Zellabweichung sein.";
}
if ($sohSpread !== null && $sohSpread > 5.0) {
    $diagnosticHints[] = "Die SOH-Spreizung zwischen den Packs ist deutlich. Einzelpack-Ansicht ist hier wichtiger als der Durchschnitt.";
}
if ($avgSoh === null) {
    $diagnosticHints[] = "Es wurden keine belastbaren Pack-SOH-Werte gelesen. Die Seite zeigt dann keine Verschleissprognose, damit kein falscher Batteriezustand entsteht.";
}
$cyclesPerYear = $systemAgeYears > 0 ? $maxCycles / $systemAgeYears : 0;
// netto_jetzt = echte nutzbare Kapazitaet (USABLE_CAPACITY in Ah*V) * SOH-Faktor
// Bei neuem BMS-Wert: netto_basis = USABLE_CAPACITY (kleiner als FCC = speicher_gross)
$netto_basis_use = isset($netto_basis) ? $netto_basis : $speicher_gross;
$netto_jetzt = $avgSoh !== null ? $netto_basis_use * ($avgSoh / 100) : null;
$generatedAtTs = isset($vitals['generated_at']) ? (int)$vitals['generated_at'] : time();
$generatedAtText = date('d.m.Y H:i:s', $generatedAtTs);
$worstPackLabel = $worstPack ? 'Schrank '.$worstPack['cabinet'].' / Pack '.$worstPack['pack'] : 'N/A';

$cabinetsData = [];
if ($vitals && !empty($vitals['cabinets'])) {
    foreach ($vitals['cabinets'] as $cab) {
        $c_cycles = $cab['cycles'] ?? 0;
        $c_soh = $cab['soh_avg'] ?? null;
        
        if ($c_cycles > 0 && $c_soh !== null) {
            $c_age = $cyclesPerYear > 0 ? $c_cycles / $cyclesPerYear : 0;
            $v_per_cycle = (100 - $c_soh) / $c_cycles;
            $v_per_year = $cyclesPerYear * $v_per_cycle;
            $projectionReliable = ($c_cycles >= 200 && $c_age >= 0.5 && $v_per_year > 0.05);
            $jBis80 = $projectionReliable ? max(0, ($c_soh - 80) / $v_per_year) : null;
            $jBis70 = $projectionReliable ? max(0, ($c_soh - 70) / $v_per_year) : null;
            $usableKwh = ((float)($cab['usable_wh'] ?? 0)) / 1000.0;
            
            $cabinetsData[] = [
                'index' => $cab['index'],
                'soh' => $c_soh,
                'cycles' => $c_cycles,
                'age' => $c_age,
                'v_per_100' => $v_per_cycle * 100,
                'v_per_year' => $v_per_year,
                'usable_kwh' => $usableKwh,
                'current_kwh' => $usableKwh > 0 ? $usableKwh * ($c_soh / 100.0) : null,
                'projection_reliable' => $projectionReliable,
                'jBis80' => $jBis80,
                'jBis70' => $jBis70
            ];
        }
    }
}
?>

<style>
@media print {
    body {
        background: #fff !important;
        color: #111 !important;
    }
    body * {
        visibility: hidden !important;
    }
    #vitals-print-scope,
    #vitals-print-scope * {
        visibility: visible !important;
    }
    #vitals-print-scope {
        position: absolute !important;
        inset: 0 auto auto 0 !important;
        width: 100% !important;
        margin: 0 !important;
        padding: 0 !important;
    }
    #vitals-print-scope .vitals-no-print,
    #vitals-print-scope .modal,
    #vitals-print-scope .btn,
    #vitals-print-scope [data-bs-toggle="modal"] .position-absolute {
        display: none !important;
    }
    #vitals-print-scope .glass-card,
    #vitals-print-scope .card {
        background: #fff !important;
        color: #111 !important;
        box-shadow: none !important;
        border-color: #bbb !important;
        break-inside: avoid;
    }
    #vitals-print-scope .row,
    #vitals-print-scope .col-12,
    #vitals-print-scope .col-md-4,
    #vitals-print-scope .col-lg-3,
    #vitals-print-scope .col-lg-5,
    #vitals-print-scope .col-lg-7 {
        display: block !important;
        width: 100% !important;
        max-width: 100% !important;
    }
    #vitals-print-scope .vital-pack-card {
        margin-bottom: 0.75rem !important;
    }
}
</style>

<script>
function saveVitalsPdf() {
    const originalTitle = document.title;
    const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, '');
    document.title = 'E3DC_Vitalwerte_' + stamp;
    window.print();
    window.setTimeout(function () {
        document.title = originalTitle;
    }, 1000);
}
</script>

<div id="vitals-print-scope" class="row fade-in">
    <div class="col-12">
        <div class="glass-card mb-4">
            <div class="d-flex justify-content-between align-items-center gap-2 flex-wrap mb-4">
                <h4 class="mb-0"><i class="fas fa-heartbeat text-danger me-2"></i> E3DC Vital Systemstatus</h4>
                <div class="vitals-no-print d-flex gap-2 flex-wrap">
                    <button type="button" class="btn btn-sm btn-outline-info" onclick="saveVitalsPdf();" title="Vitaldaten druckoptimiert als PDF speichern">
                        <i class="fas fa-file-pdf"></i> PDF speichern
                    </button>
                    <button type="button" class="btn btn-sm btn-outline-secondary" onclick="location.reload();">
                        <i class="fas fa-sync-alt"></i> Aktualisieren
                    </button>
                </div>
            </div>
            
            <?php if (!$vitals || empty($vitals['cabinets'])): ?>
                <div class="alert alert-warning">
                    <i class="fas fa-exclamation-triangle"></i> Verbindungsfehler zum RSCP oder keine Batterie-Packs gefunden.
                    <br><small><?= htmlspecialchars($output ?? 'Keine Antwort. Läuft das Python Script?') ?></small>
                </div>
            <?php else: ?>
                
                <!-- System Metriken -->
                <div class="row text-center mb-4 g-3 justify-content-center">
                    <div class="col-6 col-md-4 col-lg">
                        <div class="p-3 bg-body-secondary rounded border shadow-sm h-100">
                            <i class="fas fa-barcode text-secondary fs-3 mb-2"></i>
                            <h6 class="text-body-secondary small text-uppercase fw-bold">Seriennummer</h6>
                            <strong class="fs-6"><?= htmlspecialchars($vitals['system_info']['serial_number'] ?? 'N/A') ?></strong>
                        </div>
                    </div>
                    <div class="col-6 col-md-4 col-lg">
                        <div class="p-3 bg-body-secondary rounded border shadow-sm h-100">
                            <i class="fas fa-microchip text-info fs-3 mb-2"></i>
                            <h6 class="text-body-secondary small text-uppercase fw-bold">Modell / SW</h6>
                            <strong class="fs-6"><?= htmlspecialchars($vitals['system_info']['sw_release'] ?? 'N/A') ?></strong>
                        </div>
                    </div>
                    <div class="col-6 col-md-4 col-lg">
                        <div class="p-3 bg-body-secondary rounded border shadow-sm h-100">
                            <i class="fas fa-calendar-check text-primary fs-3 mb-2"></i>
                            <h6 class="text-body-secondary small text-uppercase fw-bold">Prod.-Datum</h6>
                            <strong class="fs-6"><?= htmlspecialchars($vitals['system_info']['production_date'] ?? 'N/A') ?></strong>
                        </div>
                    </div>
                     <div class="col-6 col-md-6 col-lg">
                        <div class="p-3 bg-body-secondary rounded border shadow-sm h-100">
                            <i class="fas fa-network-wired text-warning fs-3 mb-2"></i>
                            <h6 class="text-body-secondary small text-uppercase fw-bold">MAC Adresse</h6>
                            <strong class="fs-6 text-break" style="word-break: break-all;"><?= htmlspecialchars(substr($vitals['system_info']['mac_address'] ?? '', 0, 17)) ?></strong>
                        </div>
                    </div>
                    <div class="col-6 col-md-6 col-lg">
                        <div class="p-3 bg-body-secondary rounded border shadow-sm h-100">
                            <i class="fas fa-hdd text-success fs-3 mb-2"></i>
                            <h6 class="text-body-secondary small text-uppercase fw-bold">SSD Speicher</h6>
                            <strong class="fs-6"><?= htmlspecialchars($vitals['system_info']['disk_usage_percent'] ?? '0') ?> belegt</strong>
                        </div>
                    </div>
                </div>

                <!-- Diagnose Zusammenfassung -->
                <div class="row g-3 mb-4">
                    <div class="col-12 col-lg-4">
                        <div class="p-3 bg-body-secondary rounded border border-<?= $overallColor ?> shadow-sm h-100">
                            <div class="d-flex justify-content-between align-items-start gap-3">
                                <div>
                                    <h6 class="text-body-secondary small text-uppercase fw-bold mb-1">Diagnose-Ampel</h6>
                                    <div class="fs-4 fw-bold text-<?= $overallColor ?>"><?= htmlspecialchars($overallText) ?></div>
                                </div>
                                <i class="fas fa-shield-alt text-<?= $overallColor ?> fs-2"></i>
                            </div>
                            <p class="small text-body-secondary mb-0 mt-2">
                                Bewertet wird der schwächste Pack, nicht nur der Durchschnitt. Das ist für Supportfälle aussagekräftiger.
                            </p>
                        </div>
                    </div>
                    <div class="col-12 col-md-6 col-lg-4">
                        <div class="p-3 bg-body-secondary rounded border shadow-sm h-100">
                            <h6 class="text-body-secondary small text-uppercase fw-bold mb-2">Schwächster Pack</h6>
                            <div class="d-flex justify-content-between border-bottom pb-2 mb-2">
                                <span><?= htmlspecialchars($worstPackLabel) ?></span>
                                <strong><?= $worstPack ? fmtVital($worstPack['soh'], 1, ' %') : 'N/A' ?></strong>
                            </div>
                            <div class="d-flex justify-content-between small text-body-secondary">
                                <span>Zyklen</span>
                                <span><?= $worstPack ? number_format((int)$worstPack['cycles'], 0, ',', '.') : 'N/A' ?></span>
                            </div>
                            <div class="d-flex justify-content-between small text-body-secondary">
                                <span>Messzeitpunkt</span>
                                <span><?= htmlspecialchars($generatedAtText) ?></span>
                            </div>
                        </div>
                    </div>
                    <div class="col-12 col-md-6 col-lg-4">
                        <div class="p-3 bg-body-secondary rounded border shadow-sm h-100">
                            <h6 class="text-body-secondary small text-uppercase fw-bold mb-2">Pack-Streuung</h6>
                            <div class="d-flex justify-content-between border-bottom pb-2 mb-2">
                                <span>SOH min / max</span>
                                <strong><?= fmtVital($minSoh, 1, ' %') ?> - <?= fmtVital($maxSoh, 1, ' %') ?></strong>
                            </div>
                            <div class="d-flex justify-content-between small text-body-secondary">
                                <span>SOH-Spreizung</span>
                                <span><?= fmtVital($sohSpread, 1, ' %') ?></span>
                            </div>
                            <div class="d-flex justify-content-between small text-body-secondary">
                                <span>System-SOH</span>
                                <span><?= fmtVital($avgSoh, 1, ' %') ?> (<?= htmlspecialchars($avgSohMethod) ?>)</span>
                            </div>
                            <div class="d-flex justify-content-between small text-body-secondary">
                                <span>Max. Zelldrift</span>
                                <span><?= $maxVoltageSpread !== null ? number_format($maxVoltageSpread * 1000, 0, ',', '.').' mV' : 'N/A' ?></span>
                            </div>
                            <div class="d-flex justify-content-between small text-body-secondary">
                                <span>Max. Temp.-Spreizung</span>
                                <span><?= fmtVital($maxTempSpread, 1, ' °C') ?></span>
                            </div>
                        </div>
                    </div>
                </div>

                <?php if (!empty($diagnosticHints)): ?>
                    <div class="alert alert-<?= $overallColor ?> border-<?= $overallColor ?> shadow-sm mb-4">
                        <h6 class="fw-bold mb-2"><i class="fas fa-clipboard-check me-2"></i>Diagnose-Hinweise</h6>
                        <ul class="mb-0 ps-3">
                            <?php foreach ($diagnosticHints as $hint): ?>
                                <li><?= htmlspecialchars($hint) ?></li>
                            <?php endforeach; ?>
                        </ul>
                    </div>
                <?php endif; ?>

                <!-- Batterie Schränke -->
                <?php foreach ($vitals['cabinets'] as $cab): ?>
                    <div class="card mb-4 shadow-sm">
                        <div class="card-header d-flex justify-content-between align-items-center py-3">
                            <h5 class="mb-0">
                                <i class="fas fa-car-battery text-success me-2"></i> Batterie-Schrank <?= $cab['index'] ?>
                                <small class="text-body-secondary ms-2">(<?= $cab['count'] ?> Packs)</small>
                            </h5>
                            <span class="badge rounded-pill bg-secondary fs-6">SOH Gesamt: <?= isset($cab['soh_avg']) && $cab['soh_avg'] !== null ? fmtVital($cab['soh_avg'], 1, ' %') : 'nicht belastbar' ?></span>
                        </div>
                        <?php if (!empty($cab['usable_wh']) || !empty($cab['specified_wh'])): ?>
                            <div class="px-3 pt-3">
                                <div class="small text-body-secondary d-flex flex-wrap gap-3">
                                    <?php if (!empty($cab['specified_wh'])): ?>
                                        <span><i class="fas fa-layer-group me-1"></i>Installiert: <?= number_format($cab['specified_wh'] / 1000, 1, ',', '.') ?> kWh</span>
                                    <?php endif; ?>
                                    <?php if (!empty($cab['usable_wh'])): ?>
                                        <span><i class="fas fa-battery-three-quarters me-1"></i>Nutzbar nominal: <?= number_format($cab['usable_wh'] / 1000, 1, ',', '.') ?> kWh</span>
                                        <?php if (!empty($cab['soh_avg'])): ?>
                                            <span><i class="fas fa-heartbeat me-1"></i>Geschätzt aktuell: <?= number_format(($cab['usable_wh'] / 1000) * ((float)$cab['soh_avg'] / 100), 1, ',', '.') ?> kWh</span>
                                        <?php endif; ?>
                                    <?php endif; ?>
                                </div>
                            </div>
                        <?php endif; ?>
                        
                        <div class="card-body">
                            <div class="row g-3">
                                <?php foreach ($cab['packs'] as $pack): 
                                    $packSoh = $pack['soh'] ?? null;
                                    $packCycles = $pack['cycles'] ?? null;
                                    $packVoltageSpread = $pack['voltage_spread'] ?? null;
                                    $packTempSpread = $pack['temp_spread'] ?? null;
                                    $packVoltageMin = $pack['voltage_min'] ?? null;
                                    $packVoltageMax = $pack['voltage_max'] ?? null;
                                    $packVoltageMinCell = $pack['voltage_min_cell'] ?? null;
                                    $packVoltageMaxCell = $pack['voltage_max_cell'] ?? null;
                                    $packTempMinCell = $pack['temp_min_cell'] ?? null;
                                    $packTempMaxCell = $pack['temp_max_cell'] ?? null;
                                    $packCellVoltages = is_array($pack['cell_voltages'] ?? null) ? $pack['cell_voltages'] : [];
                                    $packCellTemps = is_array($pack['cell_temperatures'] ?? null) ? $pack['cell_temperatures'] : [];
                                    [$sohColor, $sohText] = vitalRiskClass(sohRiskLevel($packSoh));
                                    [$driftColor, $driftText] = vitalRiskClass(driftRiskLevel($packVoltageSpread));
                                    $packModalId = 'packModal_'.$cab['index'].'_'.$pack['index'];
                                    
                                    // Zelldrift Bewertung (ab 30mV oft verdächtig, ab 100mV Warnung)
                                    $driftRisk = 'text-'.$driftColor.' fw-bold';
                                ?>
                                    <div class="col-12 col-md-6 col-lg-4">
                                        <div class="card h-100 shadow-sm position-relative overflow-hidden vital-pack-card" role="button" data-bs-toggle="modal" data-bs-target="#<?= $packModalId ?>" title="Pack-Details öffnen">
                                            <!-- Color Accent Line -->
                                            <div class="position-absolute top-0 start-0 w-100 bg-<?= $sohColor ?>" style="height: 3px;"></div>
                                            
                                            <div class="card-header border-bottom d-flex justify-content-between align-items-center pt-3">
                                                <strong>Pack <?= $pack['index'] ?></strong>
                                                <span class="badge bg-<?= $sohColor ?> fw-bold px-2 py-1 fs-6 shadow-sm"><?= fmtVital($packSoh, 1, ' %') ?> SOH</span>
                                            </div>
                                            
                                            <div class="card-body p-3">
                                                <div class="d-flex justify-content-between align-items-center mb-2 pb-2 border-bottom">
                                                    <span class="text-body-secondary small text-truncate"><i class="fas fa-sync me-1"></i> Zyklen</span>
                                                    <span class="fw-bold fs-6"><?= $packCycles !== null ? number_format((int)$packCycles, 0, ',', '.') : 'N/A' ?></span>
                                                </div>
                                                
                                                <div class="d-flex justify-content-between align-items-center mb-2 pb-2 border-bottom">
                                                    <span class="text-body-secondary small text-truncate"><i class="fas fa-temperature-half text-danger me-1"></i> Temp.</span>
                                                    <span class="fw-bold small text-nowrap">
                                                        <?= isset($pack['temp_min']) ? rtrim(rtrim(number_format($pack['temp_min'], 1, '.', ''), '0'), '.') : '?' ?>°C 
                                                        <span class="text-body-secondary fw-normal mx-1">-</span> 
                                                        <?= isset($pack['temp_max']) ? rtrim(rtrim(number_format($pack['temp_max'], 1, '.', ''), '0'), '.') : '?' ?>°C
                                                    </span>
                                                </div>
                                                
                                                <div class="d-flex justify-content-between align-items-center mb-2">
                                                    <span class="text-body-secondary small text-truncate" title="Spannungsdifferenz zwischen bester und schlechtester Zelle">
                                                        <i class="fas fa-microscope text-info me-1"></i> Drift
                                                    </span>
                                                    <span class="<?= $driftRisk ?> small fw-bold text-nowrap">
                                                        <?= $packVoltageSpread !== null ? number_format($packVoltageSpread*1000, 0, ',', '.').' mV' : 'N/A' ?>
                                                    </span>
                                                </div>
                                                <?php if ($packVoltageMinCell || $packVoltageMaxCell): ?>
                                                    <div class="small text-body-secondary">
                                                        Zelle <?= htmlspecialchars((string)$packVoltageMinCell) ?> min / Zelle <?= htmlspecialchars((string)$packVoltageMaxCell) ?> max
                                                    </div>
                                                <?php endif; ?>
                                                
                                                <!-- Visual Bar for SOH Health -->
                                                <div class="progress mt-3" style="height: 8px;">
                                                    <div class="progress-bar bg-<?= $sohColor ?>" role="progressbar" style="width: <?= $packSoh !== null ? max(0, min(100, (float)$packSoh)) : 0 ?>%; opacity: 0.85;"></div>
                                                </div>
                                            </div>
                                            <div class="card-footer bg-transparent border-top small text-info">
                                                <i class="fas fa-search-plus me-1"></i> Details anzeigen
                                            </div>
                                        </div>
                                    </div>

                                    <div class="modal fade" id="<?= $packModalId ?>" tabindex="-1" aria-hidden="true">
                                        <div class="modal-dialog modal-dialog-centered">
                                            <div class="modal-content bg-body border border-<?= $sohColor ?>">
                                                <div class="modal-header">
                                                    <h5 class="modal-title">
                                                        <i class="fas fa-car-battery text-<?= $sohColor ?> me-2"></i>
                                                        Schrank <?= $cab['index'] ?> / Pack <?= $pack['index'] ?>
                                                    </h5>
                                                    <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Schließen"></button>
                                                </div>
                                                <div class="modal-body">
                                                    <div class="alert alert-<?= $sohColor ?> py-2 mb-3">
                                                        <strong>SOH-Status:</strong> <?= htmlspecialchars($sohText) ?>.
                                                        Der SOH-Wert kommt direkt aus dem BMS/DCB und wird hier nicht hochgerechnet.
                                                    </div>
                                                    <div class="row g-2 small">
                                                        <div class="col-6">SOH</div><div class="col-6 text-end fw-bold"><?= fmtVital($packSoh, 1, ' %') ?></div>
                                                        <div class="col-6">Zyklen</div><div class="col-6 text-end fw-bold"><?= $packCycles !== null ? number_format((int)$packCycles, 0, ',', '.') : 'N/A' ?></div>
                                                        <div class="col-6">Temperatur min/max</div><div class="col-6 text-end fw-bold"><?= fmtVital($pack['temp_min'] ?? null, 1, ' °C') ?> / <?= fmtVital($pack['temp_max'] ?? null, 1, ' °C') ?></div>
                                                        <div class="col-6">Temperaturspreizung</div><div class="col-6 text-end fw-bold"><?= fmtVital($packTempSpread, 1, ' °C') ?></div>
                                                        <div class="col-6">Zellspannung min/max</div><div class="col-6 text-end fw-bold"><?= fmtVital($packVoltageMin, 3, ' V') ?> / <?= fmtVital($packVoltageMax, 3, ' V') ?></div>
                                                        <div class="col-6">Zelldrift</div><div class="col-6 text-end fw-bold text-<?= $driftColor ?>"><?= $packVoltageSpread !== null ? number_format($packVoltageSpread*1000, 0, ',', '.').' mV' : 'N/A' ?> (<?= htmlspecialchars($driftText) ?>)</div>
                                                    </div>
                                                    <?php if (!empty($packCellVoltages)): ?>
                                                        <div class="mt-3">
                                                            <h6 class="small text-uppercase text-body-secondary fw-bold mb-2">Zellwerte</h6>
                                                            <div class="table-responsive" style="max-height: 220px;">
                                                                <table class="table table-sm table-striped align-middle mb-0">
                                                                    <thead><tr><th>Zelle</th><th class="text-end">Spannung</th><th class="text-end">Temperatur</th></tr></thead>
                                                                    <tbody>
                                                                        <?php foreach ($packCellVoltages as $idx => $cellV):
                                                                            $cellNo = $idx + 1;
                                                                            $cellT = $packCellTemps[$idx] ?? null;
                                                                            $isMinV = ($packVoltageMinCell === $cellNo);
                                                                            $isMaxV = ($packVoltageMaxCell === $cellNo);
                                                                            $isMinT = ($packTempMinCell === $cellNo);
                                                                            $isMaxT = ($packTempMaxCell === $cellNo);
                                                                        ?>
                                                                            <tr>
                                                                                <td>Z<?= $cellNo ?></td>
                                                                                <td class="text-end <?= ($isMinV || $isMaxV) ? 'fw-bold text-'.$driftColor : '' ?>">
                                                                                    <?= fmtVital($cellV, 3, ' V') ?>
                                                                                    <?= $isMinV ? '<span class="badge bg-secondary ms-1">min</span>' : '' ?>
                                                                                    <?= $isMaxV ? '<span class="badge bg-secondary ms-1">max</span>' : '' ?>
                                                                                </td>
                                                                                <td class="text-end <?= ($isMinT || $isMaxT) ? 'fw-bold' : '' ?>">
                                                                                    <?= fmtVital($cellT, 1, ' °C') ?>
                                                                                    <?= $isMinT ? '<span class="badge bg-secondary ms-1">min</span>' : '' ?>
                                                                                    <?= $isMaxT ? '<span class="badge bg-secondary ms-1">max</span>' : '' ?>
                                                                                </td>
                                                                            </tr>
                                                                        <?php endforeach; ?>
                                                                    </tbody>
                                                                </table>
                                                            </div>
                                                        </div>
                                                    <?php endif; ?>
                                                    <?php if ($packVoltageMinCell || $packVoltageMaxCell || $packTempMinCell || $packTempMaxCell): ?>
                                                        <div class="small text-body-secondary mt-2">
                                                            Spannung: min <?= $packVoltageMinCell ? 'Z'.$packVoltageMinCell : 'N/A' ?> / max <?= $packVoltageMaxCell ? 'Z'.$packVoltageMaxCell : 'N/A' ?>,
                                                            Temperatur: min <?= $packTempMinCell ? 'Z'.$packTempMinCell : 'N/A' ?> / max <?= $packTempMaxCell ? 'Z'.$packTempMaxCell : 'N/A' ?>
                                                        </div>
                                                    <?php endif; ?>
                                                    <?php if ($packSoh !== null && $packSoh < 80): ?>
                                                        <div class="alert alert-danger mt-3 mb-0">
                                                            <i class="fas fa-file-medical me-1"></i>
                                                            Unter 80% SOH: Werte dokumentieren, Screenshot/Export sichern und Support-Anfrage vorbereiten.
                                                        </div>
                                                    <?php endif; ?>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                <?php endforeach; ?>
                            </div>
                        </div>
                    </div>
                <?php endforeach; ?>

            <?php endif; ?>
        </div>

        <?php if ($vitals && $speicher_gross > 0 && $maxCycles > 0 && $avgSoh !== null): ?>
        <div class="glass-card mb-4 fade-in" style="animation-delay: 0.2s;">
            <div class="d-flex justify-content-between align-items-center mb-4">
                <h4 class="mb-0"><i class="fas fa-chart-line text-info me-2"></i> Degradations-Prognose & Speicherwert</h4>
            </div>
            
            <div class="row g-4">
                <!-- Netto Kapazität (Immer Gesamt) -->
                <div class="col-12">
                    <div class="card shadow-sm border-0 bg-body-secondary">
                        <div class="card-body p-4 text-center">
                            <h6 class="text-body-secondary text-uppercase fw-bold mb-3"><i class="fas fa-battery-half text-primary me-2"></i>Real Nutzkapazität Gesamtsystem</h6>
                            <h2 class="display-5 fw-bold text-body mb-2"><?= number_format($netto_jetzt, 2, ',', '.') ?> <span class="fs-4 text-muted">kWh</span></h2>
                            <p class="text-muted small">
                                Im Neuzustand nutzbar: <?= number_format($speicher_gross, 1, ',', '.') ?> kWh<br>
                                Brutto installiert (Hardware): <?= number_format($brutto_installiert, 1, ',', '.') ?> kWh
                            </p>
                            
                            <div class="progress mt-4 bg-dark-subtle shadow-sm" style="height: 24px; border-radius: 12px; font-size: 0.9rem;">
                                <div class="progress-bar bg-info text-dark fw-bold" role="progressbar" style="width: <?= $avgSoh ?>%;">Durchschnitts-SOH <?= number_format($avgSoh, 1, ',', '.') ?>%</div>
                            </div>
                        </div>
                    </div>
                </div>

                <?php foreach($cabinetsData as $cData): ?>
                <div class="col-12">
                    <div class="card shadow-sm border-2 <?= $cData['soh'] < 80 ? 'border-warning' : 'border-success' ?> bg-body-secondary mt-2">
                        <div class="card-header bg-transparent border-bottom-0 pt-3 pb-0">
                            <h5 class="fw-bold mb-0"><i class="fas fa-server me-2"></i>Schrank <?= $cData['index'] ?> <span class="badge bg-secondary ms-2"><?= $cData['cycles'] ?> Zyklen</span></h5>
                        </div>
                        <div class="card-body px-4 pb-4">
                            <div class="row g-4">
                                <!-- Degradations-Metriken (Pro Schrank) -->
                                <div class="col-12 col-lg-5">
                                    <h6 class="text-body-secondary text-uppercase fw-bold mb-3"><i class="fas fa-microscope text-danger me-2"></i>Verschleiß Analyse</h6>
                                    
                                    <div class="d-flex justify-content-between align-items-center border-bottom pb-2 mb-2">
                                        <span class="text-muted small">Aktueller SOH</span>
                                        <strong class="text-body"><?= number_format($cData['soh'], 1, ',', '.') ?> %</strong>
                                    </div>
                                    <div class="d-flex justify-content-between align-items-center border-bottom pb-2 mb-2">
                                        <span class="text-muted small">Verschleiß pro 100 Zyklen</span>
                                        <strong class="text-body"><?= number_format($cData['v_per_100'], 3, ',', '.') ?> %</strong>
                                    </div>
                                    <div class="d-flex justify-content-between align-items-center border-bottom pb-2 mb-2">
                                        <span class="text-muted small">Zyklenalter (aus Nutzung)</span>
                                        <strong class="text-body">~<?= number_format($cData['age'], 1, ',', '.') ?> Jahre</strong>
                                    </div>
                                    <?php if ($cData['current_kwh'] !== null): ?>
                                    <div class="d-flex justify-content-between align-items-center border-bottom pb-2 mb-2">
                                        <span class="text-muted small">Aktuelle Nutzkapazität</span>
                                        <strong class="text-body">~<?= number_format($cData['current_kwh'], 1, ',', '.') ?> kWh</strong>
                                    </div>
                                    <?php endif; ?>
                                    <div class="d-flex justify-content-between align-items-center">
                                        <span class="text-muted small">Jahresverlust (bei <?= number_format($cyclesPerYear, 0) ?> Zyklen/J)</span>
                                        <strong class="text-danger">~<?= number_format($cData['v_per_year'], 2, ',', '.') ?> %</strong>
                                    </div>
                                    <div class="small text-body-secondary mt-2">
                                        Prognosebasis:
                                        <?= $cData['projection_reliable'] ? '<span class="text-success fw-bold">belastbar</span>' : '<span class="text-warning fw-bold">noch unscharf</span>' ?>
                                        (linear aus SoH und Zyklen, keine Herstellergarantie-Aussage).
                                    </div>
                                </div>

                                <!-- Lebensdauer Prognose (Pro Schrank) -->
                                <div class="col-12 col-lg-7">
                                    <h6 class="text-body-secondary text-uppercase fw-bold mb-4"><i class="fas fa-calendar-check text-success me-2"></i>Lebensdauer Vorhersage (Linear)</h6>
                                    
                                    <div class="position-relative mt-2 mb-4">
                                        <!-- Base Timeline Track -->
                                        <div class="bg-dark-subtle rounded w-100" style="height: 12px; position: relative;">
                                            <?php 
                                                $year80 = fmtPrognosisYear($cData['jBis80'], $generatedAtTs);
                                                $year70 = fmtPrognosisYear($cData['jBis70'], $generatedAtTs);
                                                $date80 = fmtPrognosisMonthYear($cData['jBis80'], $generatedAtTs);
                                                $date70 = fmtPrognosisMonthYear($cData['jBis70'], $generatedAtTs);
                                                
                                                $scaleFactor = 100 / 50; 
                                                $posCurrent = (100 - $cData['soh']) * $scaleFactor;
                                                $pos80 = (100 - 80) * $scaleFactor;
                                                $pos70 = (100 - 70) * $scaleFactor;
                                            ?>
                                            
                                            <!-- Degradation Fill -->
                                            <div class="bg-success rounded border border-body" style="position: absolute; left: 0; min-width: 5px; width: <?= min(100, $posCurrent) ?>%; height: 100%; z-index: 2;" title="Aktueller Zustand"></div>
                                            <div class="bg-warning rounded-end border border-body" style="position: absolute; left: <?= min(100, $posCurrent) ?>%; width: <?= max(0, $pos80 - $posCurrent) ?>%; height: 100%; z-index: 1; opacity: 0.7;"></div>
                                            <div class="bg-danger rounded-end border border-body" style="position: absolute; left: <?= min(100, $pos80) ?>%; width: <?= max(0, $pos70 - $pos80) ?>%; height: 100%; z-index: 0; opacity: 0.5;"></div>

                                            <!-- Markers -->
                                            <div class="position-absolute" style="left: <?= min(100, $posCurrent) ?>%; top: -10px; transform: translateX(-50%); z-index: 3;">
                                                <div class="bg-light text-dark fw-bold border border-primary rounded-pill shadow-sm text-center" style="font-size: 0.7rem; padding: 2px 8px; margin-bottom: 2px;">JETZT</div>
                                                <div style="width: 2px; height: 10px; background: var(--bs-primary); margin: 0 auto;"></div>
                                            </div>

                                            <div class="position-absolute" style="left: <?= min(100, $pos80) ?>%; top: 20px; transform: translateX(-50%);">
                                                <div style="width: 2px; height: 8px; background: var(--bs-warning); margin: 0 auto;"></div>
                                                <div class="text-warning fw-bold text-center mt-1" style="font-size: 0.75rem;" title="Prognosemonat: <?= htmlspecialchars($date80) ?>"><i class="fas fa-info-circle me-1"></i>80%<br><?= htmlspecialchars($year80) ?></div>
                                            </div>

                                            <div class="position-absolute" style="left: <?= min(100, $pos70) ?>%; top: 20px; transform: translateX(-50%);">
                                                <div style="width: 2px; height: 8px; background: var(--bs-danger); margin: 0 auto;"></div>
                                                <div class="text-danger fw-bold text-center mt-1" style="font-size: 0.75rem;" title="Prognosemonat: <?= htmlspecialchars($date70) ?>"><i class="fas fa-exclamation-triangle me-1"></i>70%<br><?= htmlspecialchars($year70) ?></div>
                                            </div>
                                        </div>
                                    </div>
                                    
                                    <div class="row text-center mt-5">
                                        <div class="col-6">
                                            <h5 class="fw-bold mb-1 <?= prognosisClass($cData['jBis80'], 3) ?>"><?= fmtYears($cData['jBis80']) ?></h5>
                                            <span class="small text-muted">Bis Garantie-Grenze (80%)</span>
                                        </div>
                                        <div class="col-6">
                                            <h5 class="fw-bold mb-1 <?= prognosisClass($cData['jBis70'], 8, 5) ?>"><?= fmtYears($cData['jBis70']) ?></h5>
                                            <span class="small text-muted">Empfohlener Austausch (70%)</span>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
                <?php endforeach; ?>
            </div>
        </div>
        <?php endif; ?>
        
        <p class="text-center text-body-secondary small mt-3">
            <i class="fas fa-info-circle me-1"></i>
            Zelldrift misst die Spannungsdifferenz in Millivolt (mV). Über 50mV kann auf Zelldegradation hinweisen. Ein SOH (State of Health) repräsentiert die aktuell noch nutzbare Restkapazität im Vergleich zum Neuzustand.
        </p>
    </div>
</div>
