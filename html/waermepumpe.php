<?php
// waermepumpe.php - Optimierte Anzeige mit Wärmequelle und Soll-Werten

if (!isset($base_path) || !isset($paths)) {
    require_once 'helpers.php';
    $paths = getInstallPaths();
    $base_path = rtrim($paths['install_path'], '/') . '/';
}

// Versuche zuerst, die Ramdisk-Datei zu lesen (viel schneller!)
// ... Pfade laden wie gehabt ...

// Config laden für Editor und Typen
$conf = [];
$res = loadE3dcConfig($base_path);
if (empty($res['error'])) $conf = $res['config'];

// Defaults
$conf['luxtronik'] = $conf['luxtronik'] ?? 0;
$conf['auto_mode'] = $conf['auto_mode'] ?? 1;
$conf['wp_type'] = $conf['wp_type'] ?? -1;

$wpType = (int)$conf['wp_type'];
$pageContext = $seite ?? 'waermepumpe';
$hasNativeHeatpump = isHeatpumpEnabledConfig($conf);
$hasHeaterConfig = isHeaterEnabledConfig($conf);
$isChargingOnly = ($pageContext === 'charging')
    || ($wpType < 0 && !$hasNativeHeatpump && !$hasHeaterConfig && (string)$conf['luxtronik'] === '0');
$isHeaterPage = !$isChargingOnly && ($wpType === 2 || (!$hasNativeHeatpump && $hasHeaterConfig));

$ramdiskFile = '/var/www/html/ramdisk/luxtronik.json';
$stiebelRamdiskFile = '/var/www/html/ramdisk/stiebel_isg.json';
$dimplexRamdiskFile = '/var/www/html/ramdisk/dimplex_wpm.json';
$liveJsonFile = '/var/www/html/ramdisk/waermepumpe.json';
$json = null;

// Konfigurations-Pfad (im Installationsverzeichnis)
$isDocker = file_exists('/.dockerenv');
if ($isDocker) {
    $luxInstallDir = file_exists('/app/pi/Install/Installer/luxtronik/energy_manager.py') ? '/app/pi/Install/Installer/luxtronik/' : '/home/pi/Install/Installer/luxtronik/';
} else {
    $luxInstallDir = rtrim($paths['home_dir'], '/') . '/Install/Installer/luxtronik/';
}
$configFile = '/var/www/html/data/e3dc_v4.json'; // V4 JSON - Single Source of Truth
$serviceName = ($wpType === 4) ? 'e3dc-stiebel-live' : (($wpType === 5) ? 'e3dc-dimplex-live' : 'energy_manager'); // Name des Systemd-Services
$serviceProcessPattern = ($wpType === 4) ? 'stiebel_live.py' : (($wpType === 5) ? 'dimplex_live.py' : 'energy_manager.py');

// Dienst-Status prüfen
if ($isDocker) {
    $pid = trim(shell_exec("pgrep -f " . escapeshellarg($serviceProcessPattern)));
    $isServiceRunning = !empty($pid);
    $serviceStatus = $isServiceRunning ? 'active' : 'inactive';
} else {
    $serviceStatus = e3dcSystemdServiceStatus($serviceName);
    $isServiceRunning = ($serviceStatus === 'active');
    if (!$isServiceRunning) {
        $pid = trim(shell_exec("pgrep -f " . escapeshellarg($serviceProcessPattern) . " 2>/dev/null"));
        if ($pid !== '') {
            $isServiceRunning = true;
            $serviceStatus = 'active';
        }
    }
}

if ($wpType == 4) {
    if (file_exists($stiebelRamdiskFile)) {
        $json = json_decode(file_get_contents($stiebelRamdiskFile), true);
    } elseif (file_exists($liveJsonFile)) {
        $candidateJson = json_decode(file_get_contents($liveJsonFile), true);
        $candidateData = is_array($candidateJson) ? ($candidateJson['data'] ?? $candidateJson) : [];
        $candidateSource = strtolower((string)(
            ($candidateJson['source'] ?? '')
            . ' ' . ($candidateData['Quelle'] ?? '')
            . ' ' . ($candidateData['Hersteller'] ?? '')
        ));
        if (strpos($candidateSource, 'stiebel') !== false) {
            $json = $candidateJson;
        }
    }
} elseif ($wpType == 1 && file_exists($liveJsonFile)) {
    $json = json_decode(file_get_contents($liveJsonFile), true);
    // Hole Status-Felder aus dem Energy-Manager-Ramdisk für IDM
    if (file_exists($ramdiskFile)) {
        $emJson = json_decode(file_get_contents($ramdiskFile), true);
        foreach (['idm_surplus_kw','boost_active','pv_pause_active',
                  'price_boost_active','auto_mode',
                  'idm_ext_ww','idm_ext_hz','idm_ext_khl',
                  'idm_cooling_active','idm_external_cooling_request',
                  'idm_internal_cooling','idm_cooling_origin'] as $k) {
            if (isset($emJson[$k])) $json[$k] = $emJson[$k];
        }
    }
} elseif ($wpType == 5) {
    if (file_exists($dimplexRamdiskFile)) {
        $json = json_decode(file_get_contents($dimplexRamdiskFile), true);
    } elseif (file_exists($liveJsonFile)) {
        $candidateJson = json_decode(file_get_contents($liveJsonFile), true);
        $candidateData = is_array($candidateJson) ? ($candidateJson['data'] ?? $candidateJson) : [];
        $candidateSource = strtolower((string)(
            ($candidateJson['source'] ?? '')
            . ' ' . ($candidateData['Quelle'] ?? '')
            . ' ' . ($candidateData['Hersteller'] ?? '')
        ));
        if (strpos($candidateSource, 'dimplex') !== false) {
            $json = $candidateJson;
        }
    }
    if (is_array($json) && file_exists($ramdiskFile)) {
        $emJson = json_decode(file_get_contents($ramdiskFile), true);
        foreach (['boost_active','pv_pause_active','price_boost_active','auto_mode',
                  'dimplex_sg_state','dimplex_sg_register','dimplex_sg_address'] as $k) {
            if (isset($emJson[$k])) $json[$k] = $emJson[$k];
        }
    }
} elseif ($wpType == 0 && file_exists($ramdiskFile)) {
    $json = json_decode(file_get_contents($ramdiskFile), true);
} elseif (file_exists($liveJsonFile)) {
    // Falls der Energy Manager (noch) nicht läuft, nehmen wir die Rohdaten vom Live-Dienst
    $json = json_decode(file_get_contents($liveJsonFile), true);
}

// FALLBACK NUR NUTZEN, WENN DER DIENST AUSGESCHALTET IST
if (!$json && !$isServiceRunning && $wpType == 0) {
    $python = getPythonInterpreter();
    $script = rtrim($paths['home_dir'], '/') . '/Install/Installer/luxtronik/get_luxtronik.py';
    $cmd = escapeshellarg($python) . " " . escapeshellarg($script);
    $output = shell_exec($cmd);
    $json = json_decode($output, true);
}

// Falls immer noch kein JSON da ist, ein leeres Objekt erstellen
if (!$json) {
    $json = ['success' => false, 'error' => 'Warte auf Daten vom Hintergrunddienst...', 'data' => [], 'status' => []];
}

$data = $json['data'] ?? $json;
$status = $json['status'] ?? [];
$success = $json['success'] ?? false;
$error = $json['error'] ?? ($json['error'] ?? '');

// Neustart auslösen (Absturzsicher)
if (isset($_POST['restart_manager'])) {
    requireWebAuth(false);
    if ($isDocker) {
        $python = getPythonInterpreter();
        if ($isHeaterPage) {
            shell_exec("pkill -f 'heizstab_manager.py'");
            sleep(1);
            $hsScript = escapeshellarg(rtrim($paths['home_dir'], '/') . "/Install/Installer/heizstab_manager.py");
            shell_exec("nohup $python $hsScript > /var/www/html/logs/heizstab_manager.log 2>&1 &");
            echo "<script>window.location.href = window.location.href;</script>";
            exit;
        }
        // Beende Hauptdienst und Livedienst
        shell_exec("pkill -f 'energy_manager.py'");
        shell_exec("pkill -f 'idm_live.py'");
        shell_exec("pkill -f 'e3dc_websocket.py'");
        shell_exec("pkill -f 'stiebel_live.py'");
        shell_exec("pkill -f 'dimplex_live.py'");
        sleep(1);
        shell_exec("nohup $python {$luxInstallDir}energy_manager.py > /proc/1/fd/1 2>&1 &");
        if ($wpType === 4) {
            $stiebelScript = escapeshellarg(rtrim($paths['home_dir'], '/') . "/Install/Installer/stiebel/stiebel_live.py");
            shell_exec("nohup $python $stiebelScript > /var/www/html/logs/stiebel_live.log 2>&1 &");
        } elseif ($wpType === 5) {
            $dimplexScript = escapeshellarg(rtrim($paths['home_dir'], '/') . "/Install/Installer/dimplex/dimplex_live.py");
            shell_exec("nohup $python $dimplexScript > /proc/1/fd/1 2>&1 &");
        }
        // Der Livedienst bei Docker wird meist extern oder im Wrapper gemanagt, ein pkill reicht zum Auslösen eines Neustarts.
    } else {
        if ($isHeaterPage) {
            e3dcRunServiceWrapperAction('restart', ['e3dc-heizstab']);
            echo "<script>window.location.href = window.location.href;</script>";
            exit;
        }
        if ($wpType === 4) {
            e3dcRunServiceWrapperAction('restart', ['energy_manager', 'e3dc-stiebel-live']);
        } elseif ($wpType === 5) {
            e3dcRunServiceWrapperAction('restart', ['energy_manager', 'e3dc-dimplex-live']);
        } else {
            e3dcRunServiceWrapperAction('restart', [$serviceName, 'e3dc-idm-live', 'e3dc-lux-live']);
        }
    }
    echo "<script>window.location.href = window.location.href;</script>";
    exit;
}



// IDM Gesamt-JAZ: Berechnung direkt aus der V4-Konfiguration (idm_e_total=)
// Kein Eingabefeld im Dashboard noetig - Wert einmalig in Config setzen
$idmETotalCfg = floatval($conf['idm_e_total'] ?? 0);
$idmJazValue  = 0;
$idmWGesamt   = 0;
if ($wpType == 1 && $idmETotalCfg > 0) {
    $idmWGesamt = floatval($data['Waermemenge_Gesamt_Kum'] ?? 0);
    if ($idmWGesamt <= 0) {
        $idmWGesamt = floatval(($data['Waermemenge Heizen'] ?? $data['Wärmemenge Heizen'] ?? 0)
                             + ($data['Waermemenge Warmwasser'] ?? $data['Wärmemenge Warmwasser'] ?? 0));
    }
    if ($idmWGesamt > 0) {
        $idmJazValue = round($idmWGesamt / $idmETotalCfg, 2);
    }
}


// Zeitstempel der Dateien für die Anzeige
$configMtime = file_exists($configFile) ? date("d.m. H:i:s", filemtime($configFile)) : '--';
$pyMtime = file_exists($luxInstallDir . 'energy_manager.py') ? date("d.m. H:i:s", filemtime($luxInstallDir . 'energy_manager.py')) : '--';

// NEU: WP-Verbrauch Logik (Präzise vs. Live-Fallback)
$wp_power_w = 0;
$wp_source = 'live';

if (isset($data['Leistung_Verdichter_W']) || isset($data['Leistungsaufnahme'])) {
    $wp_power_w = $data['Leistung_Verdichter_W'] ?? (($data['Leistungsaufnahme'] ?? 0) * 1000);

    // Bei Stiebel darf Neben-/Pumpenleistung sichtbar bleiben; sie ist nur kein aktiver Verdichterbetrieb.
    if ($wpType != 4 && empty($data['Verdichter_Ein']) && empty($data['Verdichter'])) {
        $wp_power_w = 0;
    }
    $wp_source = 'modbus';
} else {
    $historyFileLive = '/var/www/html/ramdisk/live_history.txt';
    if (file_exists($historyFileLive)) {
        $lines = @file($historyFileLive, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
        if ($lines && count($lines) > 0) {
            $lastLine = json_decode(array_pop($lines), true);
            if ($lastLine && isset($lastLine['wp'])) {
                $wp_power_w = (float)$lastLine['wp'];
            }
        }
    }
}

$cop = 0;
$heiz_kw = floatval($data['Leistung_Heiz_kW'] ?? $data['Heizleistung Ist'] ?? 0);
$heiz_kw_estimated = !empty($data['stiebel_heat_power_estimated']) || !empty($data['dimplex_heat_power_estimated']) || !empty($data['wp_heat_power_estimated']);
$heiz_kw_estimate_title = '';
if ($heiz_kw_estimated) {
    $heiz_kw_estimate_title = !empty($data['dimplex_heat_power_estimated'])
        ? 'Aus elektrischer Leistung und Dimplex-COP-Schätzung berechnet'
        : 'Aus elektrischer Leistung und Stiebel-COP-Schätzung berechnet';
}
if ($wpType == 4 && $heiz_kw <= 0 && $wp_power_w > 0 && (!empty($data['Verdichter_Ein']) || !empty($data['Verdichter']))) {
    $stiebelPowerSourceForHeat = strtolower((string)($data['stiebel_power_source'] ?? ''));
    if (strpos($stiebelPowerSourceForHeat, 'dhw') !== false || strpos($stiebelPowerSourceForHeat, 'heating') !== false) {
        $stiebelCopEstimate = max(0.0, floatval($conf['stiebel_isg_cop_estimate'] ?? 3.0));
        if ($stiebelCopEstimate > 0) {
            $heiz_kw = round(($wp_power_w * $stiebelCopEstimate) / 1000.0, 3);
            $heiz_kw_estimated = true;
            $heiz_kw_estimate_title = 'Aus elektrischer Leistung und Stiebel-COP-Schätzung berechnet';
        }
    }
}
if ($wp_power_w > 0 && $heiz_kw > 0) {
    $cop = ($heiz_kw * 1000) / $wp_power_w;
}

$calc_freq = $data['Freq_Ist'] ?? 0;

function getCOPColor($val, $type) {
    if ($val <= 0) return 'text-secondary';
    if ($type == 1) { // Luft/Wasser Wärmepumpe
        if ($val >= 4.0) return 'text-success fw-bolder';
        if ($val >= 3.0) return 'text-info';
        if ($val >= 2.0) return 'text-warning';
        return 'text-danger';
    } else { // Sole/Wasser Wärmepumpe
        if ($val >= 4.8) return 'text-success fw-bolder';
        if ($val >= 3.8) return 'text-info';
        if ($val >= 2.8) return 'text-warning';
        return 'text-danger';
    }
}

$copColor = getCOPColor($cop, $wpType);
// Stats Logic (Min/Max Tracking für den Tag)
$statsFile = '/var/www/html/ramdisk/luxtronik_stats.json';
$historyFile = '/var/www/html/ramdisk/luxtronik_history.json';
$today = date('Y-m-d');
$stats = ['date' => $today, 'p_min' => null, 'p_max' => 0, 'cop_min' => null, 'cop_max' => 0, 'wm_start' => null, 'el_start' => null];

// Zuerst wm_start (und restliche Stats) aus der Datei laden, damit wir sie nicht durch History-Lücken verlieren
if (file_exists($statsFile)) {
    $tmp = @json_decode(file_get_contents($statsFile), true);
    if ($tmp && isset($tmp['date']) && $tmp['date'] === $today) {
        $stats = $tmp;
    }
}

$historyUsed = false;
if (file_exists($historyFile)) {
    $lines = @file($historyFile, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    if ($lines !== false) {
        $historyUsed = true;
        // Min/Max für den Re-Parse resetten, aber wm_start beibehalten!
        $stats['p_min'] = null;
        $stats['p_max'] = 0;
        $stats['cop_min'] = null;
        $stats['cop_max'] = 0;
        
        foreach ($lines as $line) {
            $row = json_decode($line, true);
            if (!$row || !isset($row['ts']) || strpos($row['ts'], $today) !== 0) continue;
            $d = $row['data'] ?? [];
            $p = floatval($d['Leistung_Heiz_kW'] ?? $d['Heizleistung Ist'] ?? 0);
            $v = $d['Leistung_Verdichter_W'] ?? (($d['Leistungsaufnahme'] ?? 0) * 1000);
            $c = ($v > 0 && $p > 0) ? ($p * 1000) / $v : 0;
            
            // Falls wm_start NOCH null ist, können wir ihn aus der History fischen
            $wm = floatval($d['Wärmemenge Gesamt'] ?? $d['Energie_Waerme_kWh'] ?? 0);
            if ($wm > 0 && ($stats['wm_start'] === null || $wm < $stats['wm_start'])) {
                $stats['wm_start'] = $wm;
            }

            if ($p > 0) {
                if ($stats['p_min'] === null || $p < $stats['p_min']) $stats['p_min'] = $p;
                if ($p > $stats['p_max']) $stats['p_max'] = $p;
            }
            if ($c > 0) {
                if ($stats['cop_min'] === null || $c < $stats['cop_min']) $stats['cop_min'] = $c;
                if ($c > $stats['cop_max']) $stats['cop_max'] = $c;
            }
        }
    }
}

if (isset($_POST['manual_boost']) && $wpType != 4) {
    requireWebAuth(false);
    $action = $_POST['manual_boost'] == 'on' ? 'on' : 'off';
    $python = getPythonInterpreter();
    $boostScript = rtrim($paths['home_dir'], '/') . '/Install/Installer/luxtronik/set_manual_boost.py';
    shell_exec(escapeshellarg($python) . " " . escapeshellarg($boostScript) . " " . $action);
}

if (isset($_POST['manual_ww']) && $wpType != 4) {
    requireWebAuth(false);
    $action = $_POST['manual_ww'] === 'on' ? 'on' : 'off';
    $flagFile = '/var/www/html/ramdisk/manual_ww_boost.flag';
    if ($action === 'on') {
        file_put_contents($flagFile, time());
        @chmod($flagFile, 0666);
    } else {
        if (file_exists($flagFile)) @unlink($flagFile);
    }
}

if (isset($_POST['toggle_auto_mode'])) {
    requireWebAuth(false);
    $new_mode = (int)$_POST['toggle_auto_mode'];
    // V4 JSON schreiben (Single Source of Truth)
    $v4Path = '/var/www/html/data/e3dc_v4.json';
    $v4Data = @json_decode(@file_get_contents($v4Path), true) ?? [];
    $v4Data['auto_mode'] = (string)$new_mode;
    $json = json_encode($v4Data, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES);
    if ($json !== false) e3dcWriteJsonAtomic($v4Path, $json);
    // Ramdisk-Cache invalidieren
    @unlink('/var/www/html/ramdisk/e3dc_config_cache.json');
    if ($isDocker) {
        shell_exec("pkill -f 'energy_manager.py'");
        sleep(1);
        shell_exec("nohup $python {$luxInstallDir}energy_manager.py > /var/www/html/logs/energy_manager.log 2>&1 &");
    } else {
        e3dcRunServiceWrapperAction('restart', [$serviceName]);
    }
    
    echo "<script>window.location.href = window.location.href;</script>";
    exit;
}

$manualBoostActive = file_exists('/var/www/html/ramdisk/manual_boost.flag');
$manualWwActive = file_exists('/var/www/html/ramdisk/manual_ww_boost.flag');
if ($success) {
    $currP = $heiz_kw;
    if ($currP > 0) {
        if ($stats['p_min'] === null || $currP < $stats['p_min']) $stats['p_min'] = $currP;
        if ($currP > $stats['p_max']) $stats['p_max'] = $currP;
    }
    if ($cop > 0) {
        if ($stats['cop_min'] === null || $cop < $stats['cop_min']) $stats['cop_min'] = $cop;
        if ($cop > $stats['cop_max']) $stats['cop_max'] = $cop;
    }
    
    // Tages-Startwerte fuer Waermemenge UND elektrische Energie (IDM-eigene Zaehler)
    $current_wm = floatval($data['Wärmemenge Gesamt'] ?? $data['Energie_Waerme_kWh'] ?? 0);
    if ($current_wm > 0 && ($stats['wm_start'] === null || $stats['wm_start'] == 0)) {
        $stats['wm_start'] = $current_wm;
    }
    $current_el = floatval($data['Leistungsaufnahme_Gesamt'] ?? $data['Energie_Elek_kWh'] ?? 0);
    if ($current_el > 0 && ($stats['el_start'] === null || $stats['el_start'] == 0)) {
        $stats['el_start'] = $current_el;
    }

    // Immer speichern, damit wm_start + el_start ueberleben
    file_put_contents($statsFile, json_encode($stats));
}

$p_min_disp = ($stats['p_min'] !== null) ? number_format($stats['p_min'], 1, ',', '.') : '--';
$p_max_disp = number_format($stats['p_max'], 1, ',', '.');
$cop_min_disp = ($stats['cop_min'] !== null) ? number_format($stats['cop_min'], 2, ',', '.') : '--';
$cop_max_disp = number_format($stats['cop_max'], 2, ',', '.');

function fmtVal($val, $unit='') {
    if (!isset($val) || $val === '--' || $val === null) return '--';
    return number_format((float)$val, 1, ',', '.') . $unit;
}

function wpFirstVal($data, $keys, $default=null) {
    foreach ($keys as $key) {
        if (array_key_exists($key, $data) && $data[$key] !== null && $data[$key] !== '') {
            return $data[$key];
        }
    }
    return $default;
}

function fmtDecTime($val) {
    if (!isset($val)) return '--:--';
    $h = floor((float)$val);
    $m = round(((float)$val - $h) * 60);
    return sprintf("%02d:%02d", $h, $m);
}

$jaz = 0;
$jazLabel = "Tages-AZ";

// Werte aus IDM oder Luxtronik
$waerme_heute  = floatval($data['Wärmemenge Gesamt'] ?? $data['Energie_Waerme_kWh'] ?? $data['Waerme_Tag_kWh'] ?? 0);
$elek_heute    = floatval($data['Leistungsaufnahme_Gesamt'] ?? $data['Energie_Elek_kWh'] ?? $data['Strom_Tag_kWh'] ?? 0);

if ($wpType == 4) {
    $stiebelWaermeTag = floatval($data['Waerme_Tag_kWh'] ?? 0);
    $stiebelStromTag = floatval($data['Strom_Tag_kWh'] ?? 0);
    if ($stiebelWaermeTag > 0 && $stiebelStromTag > 0.05) {
        $jaz = $stiebelWaermeTag / $stiebelStromTag;
    }
}

// el_start beim ersten Aufruf des Tages setzen (wm_start wird oben gespeichert)
if ($elek_heute > 0 && ($stats['el_start'] === null || $stats['el_start'] == 0)) {
    $stats['el_start'] = $elek_heute;
    file_put_contents($statsFile, json_encode($stats));
}

$tagesWaerme = ($stats['wm_start'] > 0 && $waerme_heute >= $stats['wm_start']) ? ($waerme_heute - $stats['wm_start']) : 0;
$tagesElektrisch = ($stats['el_start'] > 0 && $elek_heute >= $stats['el_start']) ? ($elek_heute - $stats['el_start']) : 0;

if ($wpType != 4 && $tagesElektrisch > 0.05 && $tagesWaerme > 0) {
    $jaz = $tagesWaerme / $tagesElektrisch;
}

$gesamtJaz = 0;
if ($elek_heute > 0) {
    $gesamtJaz = $waerme_heute / $elek_heute;
}


function getModeText($val) {
    if (!isset($val)) return '--';
    $modes = [0 => 'Aus', 1 => 'Setpoint', 2 => 'Offset', 3 => 'Level'];
    return $modes[$val] ?? '--';
}

$autoMode = $json['auto_mode'] ?? $conf['auto_mode'];

// Heizstab Manager Daten laden (wp_type=2 oder Heizstab ohne native WP)
$hsData = [];
$hsServiceRunning = false;
$hsManualFile = '/var/www/html/ramdisk/heizstab_manual_override.json';
$hsManualOverride = null;
if ($isHeaterPage) {
    $hsFile = '/var/www/html/ramdisk/heizstab_data.json';
    if (file_exists($hsFile) && (time() - filemtime($hsFile)) < 60) {
        $hsData = json_decode(file_get_contents($hsFile), true) ?: [];
    }
    if (file_exists($hsManualFile)) {
        $tmpManual = @json_decode(@file_get_contents($hsManualFile), true);
        if (is_array($tmpManual)) {
            $expires = (int)($tmpManual['expires_ts'] ?? 0);
            if ($expires > 0 && time() > $expires) {
                @unlink($hsManualFile);
            } else {
                $hsManualOverride = $tmpManual;
            }
        }
    }
    if ($isDocker) {
        $hsPid = trim(shell_exec("pgrep -f 'heizstab_manager.py'"));
        $hsServiceRunning = !empty($hsPid);
    } else {
        $hsServiceStatus = e3dcSystemdServiceStatus('e3dc-heizstab');
        $hsServiceRunning = ($hsServiceStatus === 'active');
    }
}

// POST: Heizstab Auto-Modus umschalten
if (isset($_POST['toggle_hs_auto'])) {
    requireWebAuth(false);
    $new_mode = (int)$_POST['toggle_hs_auto'];
    // V4 JSON schreiben (Single Source of Truth)
    $v4Path = '/var/www/html/data/e3dc_v4.json';
    $v4Data = @json_decode(@file_get_contents($v4Path), true) ?? [];
    $v4Data['hs_auto_mode'] = (string)$new_mode;
    $json = json_encode($v4Data, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES);
    if ($json !== false) e3dcWriteJsonAtomic($v4Path, $json);
    // Ramdisk-Cache invalidieren
    @unlink('/var/www/html/ramdisk/e3dc_config_cache.json');
    echo "<script>window.location.href = window.location.href;</script>";
    exit;
}

// POST: Heizstab manuell Vollgas / zurueck zur normalen Regelung
if (isset($_POST['hs_manual_full']) || isset($_POST['hs_manual_auto'])) {
    requireWebAuth(false);
    if (isset($_POST['hs_manual_full'])) {
        $ttlHours = 2;
        $payload = [
            'mode' => 'full',
            'created_ts' => time(),
            'expires_ts' => time() + ($ttlHours * 3600),
            'source' => 'webui',
        ];
        @file_put_contents($hsManualFile . '.tmp', json_encode($payload, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES));
        @rename($hsManualFile . '.tmp', $hsManualFile);
        @chmod($hsManualFile, 0666);
    } else {
        @unlink($hsManualFile);
    }
    echo "<script>window.location.href = window.location.href;</script>";
    exit;
}


if ($isChargingOnly) {
    $cardTitle = 'Intelligentes Lademanagement';
    $wpIcon = 'fa-charging-station';
} elseif ($isHeaterPage) {
    $cardTitle = 'Heizstab / Shelly-Heizluefter';
    $wpIcon = 'fa-fire-burner';
} elseif ($wpType == 4) {
    $cardTitle = 'Stiebel Eltron ISG';
    $wpIcon = 'fa-water';
} elseif ($wpType == 5) {
    $cardTitle = 'Dimplex WPM Touch';
    $wpIcon = 'fa-temperature-three-quarters';
} else {
    $cardTitle = ($wpType == 1) ? 'IDM Wärmepumpe' : 'Luxtronik Wärmepumpe';
    $wpIcon = 'fa-fire-alt';
}

$displaySuccess = $success;
$displayServiceRunning = $isServiceRunning;
$displayServiceStatus = $serviceStatus ?: 'inactive';
$displayServiceLabel = 'Energy-Manager';
if ($isChargingOnly) {
    $displaySuccess = true;
    $displayServiceRunning = true;
    $displayServiceStatus = 'bereit';
    $displayServiceLabel = 'Ladeplanung';
} elseif ($isHeaterPage) {
    $displaySuccess = $hsServiceRunning && !empty($hsData['success']);
    $displayServiceRunning = $hsServiceRunning;
    $displayServiceStatus = $hsServiceRunning ? 'active' : 'inactive';
    $displayServiceLabel = 'Heizstab-Dienst';
} elseif ($wpType == 4) {
    $displayServiceLabel = 'Stiebel ISG Live';
} elseif ($wpType == 5) {
    $displayServiceLabel = 'Dimplex WPM Live';
}
?>

<div id="luxtronik-card" class="card shadow-sm mb-4" style="border-radius: 16px;">
    <div class="card-body p-3">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <h5 class="card-title text-info fw-bold m-0"><i class="fas <?= $wpIcon ?> me-2"></i><?= $cardTitle ?></h5>
            <div>
                <?php if($displaySuccess): ?>
                    <span class="badge bg-success" style="cursor:pointer;" onclick="showDiagnoseLog('wp_raw')" title="Rohdaten anzeigen"><?= $isChargingOnly ? 'Bereit' : 'Verbunden' ?></span>
                    <?php if($autoMode == 0): ?>
                        <span class="badge bg-secondary ms-1">Automatik Aus</span>
                    <?php endif; ?>
                <?php else: ?>
                    <span class="badge bg-danger" style="cursor:pointer;" onclick="showDiagnoseLog('wp_raw')" title="Rohdaten anzeigen">Fehler</span>
                <?php endif; ?>
            </div>
        </div>

        <?php if ($isChargingOnly): ?>
            <div class="alert alert-info py-2 small shadow-sm">
                <i class="fas fa-info-circle me-2"></i>
                Ladeplanung läuft über Storage- und Wallbox-Manager. Eine native Wärmepumpe ist dafür nicht erforderlich.
            </div>
        <?php elseif ($isHeaterPage):

            // --- Heizstab / myPV AC ELWA-E Dashboard (wp_type=2) ---
            $hsSuccess   = !empty($hsData['success']);
            $hsPowerW    = (int)($hsData['Heizstab_Power'] ?? 0);
            $hsSetpointW = (int)($hsData['elwa_setpoint_w'] ?? $hsPowerW);
            $waterTemp   = isset($hsData['elwa_water_temp_c']) ? (float)$hsData['elwa_water_temp_c'] : null;
            $targetTemp  = isset($hsData['elwa_target_temp_c']) ? (float)$hsData['elwa_target_temp_c'] : null;
            $elwaStatus  = $hsData['elwa_status'] ?? '';
            $shellyOn    = !empty($hsData['shelly_heiz_on']);
            $shellyW     = (float)($hsData['shelly_heiz_w'] ?? 0);
            $hsMode      = $hsData['hs_mode'] ?? 'unknown';
            $hsReason    = $hsData['hs_reason'] ?? '--';
            $hsSurplus   = (int)($hsData['surplus_w'] ?? 0);
            $hsSoc       = (float)($hsData['soc'] ?? 0);
            $hsAutoOn    = ($hsMode === 'pv_auto');
            $hsAutoConf  = (int)($conf['hs_auto_mode'] ?? 1);
            $isElwa      = (strtolower($conf['heizstab_type'] ?? 'generic') === 'mypv_elwa');
            $hsMaxW      = (int)($conf['heizstab_max_w'] ?? 3000);
            $powerPct    = $hsMaxW > 0 ? min(100, round($hsPowerW / $hsMaxW * 100)) : 0;
            $manualMode  = is_array($hsManualOverride) ? strtolower((string)($hsManualOverride['mode'] ?? '')) : '';
            $manualFullActive = ($manualMode === 'full') || ($hsMode === 'manual_full');
            $manualUntil = isset($hsManualOverride['expires_ts']) ? date('H:i', (int)$hsManualOverride['expires_ts']) : '';
        ?>

            <?php if (!$hsServiceRunning): ?>
                <div class="alert alert-warning py-2 small">
                    <i class="fas fa-exclamation-triangle me-2"></i>
                    <strong>e3dc-heizstab</strong> Dienst ist nicht aktiv.
                    Bitte deployen und: <code>sudo systemctl enable --now e3dc-heizstab</code>
                </div>
            <?php elseif (!$hsSuccess): ?>
                <div class="alert alert-info py-2 small">
                    <i class="fas fa-spinner fa-spin me-2"></i> Warte auf Daten vom heizstab_manager...
                </div>
            <?php else: ?>

                <?php if ($isElwa && $waterTemp !== null): ?>
                <!-- ELWA-E Temperatur-Block (wie Luxtronik WP-Kacheln) -->
                <div class="row row-cols-2 row-cols-md-4 g-2 justify-content-center mb-3">
                    <div class="col text-center">
                        <div class="p-2 bg-body-tertiary rounded border h-100 d-flex flex-column justify-content-center">
                            <div class="small text-muted">Wassertemp. <span class="fw-normal">(Ist/Soll)</span></div>
                            <div class="fw-bold text-warning">
                                <?= number_format($waterTemp, 1, ',', '.') ?>°C
                                <?php if ($targetTemp !== null): ?>
                                <span class="text-muted small fw-normal">/ <?= number_format($targetTemp, 1, ',', '.') ?>°C</span>
                                <?php endif; ?>
                            </div>
                        </div>
                    </div>
                    <div class="col text-center">
                        <div class="p-2 bg-body-tertiary rounded border h-100 d-flex flex-column justify-content-center">
                            <div class="small text-muted"><i class="fas fa-fire-burner me-1"></i>Heizstab</div>
                            <div class="fw-bold <?= $hsPowerW > 0 ? 'text-warning' : 'text-secondary' ?>">
                                <?= number_format($hsPowerW / 1000, 2, ',', '.') ?> kW
                            </div>
                            <div class="small text-muted">Istleistung</div>
                        </div>
                    </div>
                    <div class="col text-center">
                        <div class="p-2 bg-body-tertiary rounded border h-100 d-flex flex-column justify-content-center">
                            <div class="small text-muted"><i class="fas fa-solar-panel me-1"></i>PV-Überschuss</div>
                            <div class="fw-bold <?= $hsSurplus > 0 ? 'text-success' : 'text-secondary' ?>">
                                <?= number_format($hsSurplus) ?> W
                            </div>
                        </div>
                    </div>
                    <div class="col text-center">
                        <div class="p-2 bg-body-tertiary rounded border h-100 d-flex flex-column justify-content-center">
                            <div class="small text-muted"><i class="fas fa-battery-half me-1"></i>Speicher</div>
                            <div class="fw-bold"><?= number_format($hsSoc, 0) ?> %</div>
                        </div>
                    </div>
                </div>
                <?php else: ?>
                <!-- Generic Heizstab: kompakte 4er-Kacheln -->
                <div class="row g-2 mb-3">
                    <div class="col-6 col-md-3">
                        <div class="p-2 bg-body-tertiary rounded border text-center h-100">
                            <div class="small text-muted"><i class="fas fa-fire-burner me-1"></i>Heizstab</div>
                            <div class="fw-bold <?= $hsPowerW > 0 ? 'text-warning' : 'text-secondary' ?>"><?= number_format($hsPowerW / 1000, 2, ',', '.') ?> kW</div>
                            <div class="small text-muted">Istleistung</div>
                        </div>
                    </div>
                    <div class="col-6 col-md-3">
                        <div class="p-2 bg-body-tertiary rounded border text-center h-100">
                            <div class="small text-muted"><i class="fas fa-plug me-1"></i>Shelly</div>
                            <?php if (!empty($conf['shelly_heiz_ip']) && $conf['shelly_heiz_ip'] !== '0.0.0.0'): ?>
                                <div class="fw-bold <?= $shellyOn ? 'text-success' : 'text-secondary' ?>">
                                    <?= $shellyOn ? 'EIN' : 'AUS' ?>
                                    <?php if ($shellyW > 0): ?><span class="small fw-normal">&nbsp;<?= number_format($shellyW, 0) ?>W</span><?php endif; ?>
                                </div>
                            <?php else: ?>
                                <div class="text-muted small">nicht konf.</div>
                            <?php endif; ?>
                        </div>
                    </div>
                    <div class="col-6 col-md-3">
                        <div class="p-2 bg-body-tertiary rounded border text-center h-100">
                            <div class="small text-muted"><i class="fas fa-solar-panel me-1"></i>PV-Überschuss</div>
                            <div class="fw-bold <?= $hsSurplus > 0 ? 'text-success' : 'text-secondary' ?>"><?= number_format($hsSurplus) ?> W</div>
                        </div>
                    </div>
                    <div class="col-6 col-md-3">
                        <div class="p-2 bg-body-tertiary rounded border text-center h-100">
                            <div class="small text-muted"><i class="fas fa-robot me-1"></i>Modus</div>
                            <?php if ($hsAutoOn): ?>
                                <span class="badge bg-success">PV-Auto</span>
                            <?php else: ?>
                                <span class="badge bg-secondary">Manuell</span>
                            <?php endif; ?>
                        </div>
                    </div>
                </div>
                <?php endif; ?>

                <!-- Leistungsbalken -->
                <div class="mb-3">
                    <div class="d-flex justify-content-between small text-muted mb-1">
                        <span><i class="fas fa-bolt me-1"></i>Leistung</span>
                        <span><?= number_format($hsPowerW) ?> W / <?= number_format($hsMaxW) ?> W Max</span>
                    </div>
                    <div class="progress" style="height: 8px; border-radius: 4px;">
                        <div class="progress-bar <?= $hsPowerW > 0 ? 'bg-warning' : 'bg-secondary' ?>"
                             style="width: <?= $powerPct ?>%; transition: width 1s ease;"></div>
                    </div>
                </div>

                <!-- Aktueller Status -->
                <div class="row g-2 mb-3">
                    <div class="col-12 col-xl-7">
                        <h6 class="text-muted text-uppercase small fw-bold mb-2">Aktueller Status</h6>
                        <div class="p-2 bg-body-tertiary rounded border d-flex flex-wrap align-items-center gap-2" style="min-height: 46px;">
                            <?php if ($elwaStatus === 'Heizen'): ?>
                                <span class="badge bg-warning text-dark">
                                    <i class="fas fa-fire me-1"></i> Heizen
                                </span>
                            <?php elseif ($elwaStatus === 'Boost'): ?>
                                <span class="badge bg-danger pulsating">
                                    <i class="fas fa-bolt me-1"></i> Boost
                                </span>
                            <?php elseif ($elwaStatus === 'Standby'): ?>
                                <span class="badge bg-secondary">Standby</span>
                            <?php elseif ($elwaStatus === 'Fertig'): ?>
                                <span class="badge bg-success"><i class="fas fa-check me-1"></i>Fertig</span>
                            <?php elseif (strpos((string)$elwaStatus, 'Fehler') === 0): ?>
                                <span class="badge bg-danger"><i class="fas fa-exclamation-triangle me-1"></i>Fehler</span>
                            <?php else: ?>
                                <span class="badge bg-secondary">--</span>
                            <?php endif; ?>

                            <?php if ($hsAutoOn): ?>
                                <span class="badge bg-info text-dark"><i class="fas fa-magic me-1"></i>PV-Auto aktiv</span>
                            <?php endif; ?>

                            <?php if ($manualFullActive): ?>
                                <span class="badge bg-warning text-dark"><i class="fas fa-bolt me-1"></i>Manuell Vollgas<?= $manualUntil ? ' bis ' . htmlspecialchars($manualUntil) : '' ?></span>
                            <?php endif; ?>

                            <?php if (!empty($conf['shelly_heiz_ip']) && $conf['shelly_heiz_ip'] !== '0.0.0.0' && $shellyOn): ?>
                                <span class="badge bg-success"><i class="fas fa-plug me-1"></i>Shelly EIN <?= $shellyW > 0 ? '(' . number_format($shellyW, 0) . 'W)' : '' ?></span>
                            <?php endif; ?>
                        </div>
                    </div>
                    <div class="col-12 col-xl-5">
                        <h6 class="text-muted text-uppercase small fw-bold mb-2">Grund</h6>
                        <div class="p-2 bg-body-tertiary rounded border small" style="min-height: 46px; display: flex; align-items: center;">
                            <i class="fas fa-info-circle me-1 text-info"></i>
                            <?= htmlspecialchars($hsReason) ?>
                        </div>
                    </div>
                </div>

                <!-- ELWA-E Modbus Set-Werte -->
                <?php if ($isElwa): ?>
                <h6 class="text-muted text-uppercase small fw-bold mb-2">Modbus Set-Werte (Vorgaben)</h6>
                <div class="row g-2 mb-3">
                    <div class="col-6">
                        <div class="p-2 bg-body-tertiary rounded border text-center <?= $hsSetpointW > 0 ? 'border-warning border-opacity-50' : '' ?>">
                            <div class="small text-muted mb-1"><i class="fas fa-sliders-h me-1"></i>Sollleistung</div>
                            <span class="badge <?= $hsSetpointW > 0 ? 'bg-warning text-dark' : 'bg-secondary opacity-50' ?>"
                                  title="Register 1000: Sollleistung Modbus">
                                <?= number_format($hsSetpointW) ?> W
                            </span>
                        </div>
                    </div>
                    <div class="col-6">
                        <div class="p-2 bg-body-tertiary rounded border text-center">
                            <div class="small text-muted mb-1"><i class="fas fa-clock me-1"></i>Modbus-Timeout</div>
                            <span class="badge bg-success opacity-75" title="Register 1004: Modbus-Timeout (sollte 60s sein)">
                                60 s
                            </span>
                        </div>
                    </div>
                </div>
                <?php endif; ?>

                <!-- Steuerung -->
                <div class="d-flex gap-2 mb-2">
                    <form method="post" class="flex-grow-1">
                        <?php if ($hsAutoConf == 1): ?>
                            <button type="submit" name="toggle_hs_auto" value="0" class="btn btn-outline-success btn-sm w-100 fw-bold">
                                <i class="fas fa-check-circle me-1"></i> PV-AUTO AN
                            </button>
                        <?php else: ?>
                            <button type="submit" name="toggle_hs_auto" value="1" class="btn btn-secondary btn-sm w-100 fw-bold">
                                <i class="fas fa-pause-circle me-1"></i> PV-AUTO AUS
                            </button>
                        <?php endif; ?>
                    </form>
                    <form method="post" class="flex-grow-1">
                        <?php if ($manualFullActive): ?>
                            <button type="submit" name="hs_manual_auto" value="1" class="btn btn-outline-info btn-sm w-100 fw-bold">
                                <i class="fas fa-rotate-left me-1"></i> Vollgas aus / Auto
                            </button>
                        <?php else: ?>
                            <button type="submit" name="hs_manual_full" value="1" class="btn btn-outline-warning btn-sm w-100 fw-bold">
                                <i class="fas fa-bolt me-1"></i> Vollgas 2h
                            </button>
                        <?php endif; ?>
                    </form>
                    <a href="<?= getContextPageUrl('config', ['expand' => 'luxtronik']) ?>#group-luxtronik" class="btn btn-sm btn-outline-secondary">
                        <i class="fas fa-cog"></i>
                    </a>
                </div>

            <?php endif; // $hsSuccess ?>



        <?php else: // Luxtronik / IDM ?>
            <?php if (!$success): ?>
                <div class="alert alert-info d-flex align-items-center small py-2">
                    <i class="fas fa-spinner fa-spin me-2"></i> <strong>Wärmepumpe wird abgefragt...</strong>
                </div>
            <?php endif; ?>

            <?php if ($success): ?>
                <?php if ($wpType == 4): ?>
                <div class="alert alert-info small py-2 mb-3">
                    <i class="fas fa-shield-alt me-1"></i>
                    Stiebel ISG Live liest nur Messwerte. SG-Ready-Schreiben bleibt im Wärmepumpen-Manager separat abgesichert.
                </div>
                <?php else: ?>
                <div class="mb-3 d-flex gap-2">
                    <form method="post" class="flex-grow-1 d-flex gap-2">
                        <?php if ($manualBoostActive): ?>
                            <button type="submit" name="manual_boost" value="off" class="btn btn-danger btn-sm w-100 fw-bold"><i class="fas fa-hand-paper me-1"></i> BOOST STOPPEN</button>
                        <?php else: ?>
                            <button type="submit" name="manual_boost" value="on" class="btn btn-outline-warning btn-sm w-100 fw-bold"><i class="fas fa-bolt me-1"></i> BOOST (AKKU LEEREN)</button>
                        <?php endif; ?>
                        
                        <?php if ($manualWwActive): ?>
                            <button type="submit" name="manual_ww" value="off" class="btn btn-danger btn-sm w-100 fw-bold"><i class="fas fa-stop me-1"></i> WW-SOFORT STOPPEN</button>
                        <?php else: ?>
                            <!-- Das Warmwasser-Button als zweites Element in der Flexbox -->
                            <button type="submit" name="manual_ww" value="on" class="btn btn-outline-danger btn-sm w-100 fw-bold" title="Warmwasser für 2 Stunden auf Maximum (inkl. Zirkulation)"><i class="fas fa-hot-tub me-1"></i> 1x WARM WASSER</button>
                        <?php endif; ?>
                    </form>
                    <form method="post">
                        <?php if ($autoMode == 1): ?>
                            <button type="submit" name="toggle_auto_mode" value="0" class="btn btn-outline-success btn-sm h-100 fw-bold" title="PV Überschuss-Automatik abschalten">
                                <i class="fas fa-check-circle me-1"></i> AUTO 
                            </button>
                        <?php else: ?>
                            <button type="submit" name="toggle_auto_mode" value="1" class="btn btn-secondary btn-sm h-100 fw-bold" title="PV Überschuss-Automatik einschalten">
                                <i class="fas fa-pause-circle me-1"></i> AUTO AUS
                            </button>
                        <?php endif; ?>
                    </form>
                </div>
                <?php endif; ?>

                                <?php
                    // Display logic for Kaeltespeicher
                    $khlSoll = floatval($conf['khl'] ?? $conf['kuehlsoll'] ?? 0);
                    if ($wpType == 1) {
                        $istKhlTemp = isset($data['Kaeltespeicher_Ist']) && !is_null($data['Kaeltespeicher_Ist']) ? $data['Kaeltespeicher_Ist'] : null;
                    } else {
                        $istKhlTemp = $data['Kaeltespeicher_Ist'] ?? $data['Kaeltespeicher_Temp'] ?? null;
                    }
                    $showKhl = ($khlSoll > 0 || !is_null($istKhlTemp));
                    $colClass = $showKhl ? 'row-cols-2 row-cols-md-5' : 'row-cols-2 row-cols-md-4';
                ?>
                <div class="row <?= $colClass ?> g-2 justify-content-center mb-3">
                    <div class="col text-center">
                        <div class="p-2 bg-body-tertiary rounded border h-100 d-flex flex-column justify-content-center">
                            <div class="small text-muted">Außen <span class="fw-normal">(Ist/Mittel)</span></div>
                            <?php 
                                $a_ist = $data['Außentemperatur'] ?? ($data['Aussentemp'] ?? null);
                                $a_mittel_raw = $data['Außentemperatur_Mittel'] ?? ($data['Aussentemp_Mittel'] ?? ($data['Aussen_Mittel'] ?? null));
                                $season_raw = $a_mittel_raw ?? ($data['wp_season_temp'] ?? $a_ist);
                                $a_mittel = is_numeric($season_raw) ? floatval($season_raw) : -99;
                                $hg = floatval($data['Heizgrenze_Temperatur'] ?? ($data['Heizgrenze'] ?? 16.0));
                                $ist_sommer = ($a_mittel !== -99 && $a_mittel >= $hg);
                                $ist_winter = ($a_mittel !== -99 && $a_mittel < $hg);
                            ?>
                            <div class="fw-bold"><?= fmtVal($a_ist, '°C') ?>
                                <span class="text-muted small fw-normal">/ <?= fmtVal($a_mittel_raw, '°C') ?></span>
                                <?php if ($ist_sommer): ?>
                                    <span class="badge bg-warning text-dark border border-warning" style="font-size:0.55rem; vertical-align: middle;">SOMMER</span>
                                <?php elseif ($ist_winter): ?>
                                    <span class="badge bg-info text-dark border border-info" style="font-size:0.55rem; vertical-align: middle;">WINTER</span>
                                <?php endif; ?>
                            </div>
                        </div>
                    </div>
                    <div class="col text-center">
                        <div class="p-2 bg-body-tertiary rounded border h-100 d-flex flex-column justify-content-center">
                            <div class="small text-muted">Vorlauf <?php if ($wpType == 1): ?><span class="fw-normal">(Ist/Soll)</span><?php else: ?><span class="fw-normal">(Ist)</span><?php endif; ?></div>
                            <div class="fw-bold text-danger">
                                <?= fmtVal($data['Vorlauf_Ist'] ?? ($data['Vorlauf'] ?? null), '°C') ?>
                                <?php if ($wpType == 1): ?>
                                <span class="text-muted small fw-normal">/ <?= fmtVal($data['Vorlauf_Soll'] ?? ($data['Vorlauf-Soll'] ?? null), '°C') ?></span>
                                <?php endif; ?>
                            </div>
                        </div>
                    </div>
                    <div class="col text-center">
                        <div class="p-2 bg-body-tertiary rounded border h-100 d-flex flex-column justify-content-center">
                            <div class="small text-muted">Rücklauf <?php if ($wpType == 4): ?><span class="fw-normal">(Ist/HK-Soll)</span><?php elseif ($wpType != 1): ?><span class="fw-normal">(Ist/Soll)</span><?php endif; ?></div>
                            <div class="fw-bold text-info"><?= fmtVal($data['Ruecklauf_Ist'] ?? ($data['Rücklauf'] ?? null), '°C') ?>
                                <?php if ($wpType == 4): ?>
                                <span class="text-muted small fw-normal">/ <?= fmtVal($data['Heizkreis1_Soll'] ?? ($data['wp_heating_circuit_soll'] ?? null), '°C') ?></span>
                                <?php elseif ($wpType != 1): ?>
                                <span class="text-muted small fw-normal">/ <?= fmtVal($data['Ruecklauf_Soll'] ?? ($data['Rücklauf-Soll'] ?? null), '°C') ?></span>
                                <?php endif; ?>
                            </div>
                        </div>
                    </div>
                    <div class="col text-center">
                        <div class="p-2 bg-body-tertiary rounded border h-100 d-flex flex-column justify-content-center">
                            <div class="small text-muted">Warmwasser <span class="fw-normal">(Ist/Soll)</span></div>
                            <div class="fw-bold text-warning"><?= fmtVal($data['Warmwasser_Ist'] ?? ($data['Warmwasser-Ist'] ?? null), '°C') ?>
                                <span class="text-muted small fw-normal">/ <?= fmtVal($data['Warmwasser_Soll'] ?? ($data['Warmwasser-Soll'] ?? null), '°C') ?></span>
                            </div>
                        </div>
                    </div>
                    <?php if ($showKhl): ?>
                    <div class="col text-center">
                        <div class="p-2 bg-body-tertiary rounded border border-primary border-opacity-50 h-100 d-flex flex-column justify-content-center">
                            <div class="small text-muted">Kältespeicher <span class="fw-normal">(Ist<?= $khlSoll > 0 ? '/Soll' : '' ?>)</span></div>
                            <div class="fw-bold text-primary"><?= fmtVal($istKhlTemp, '°C') ?>
                                <?php if ($khlSoll > 0): ?>
                                <span class="text-muted small fw-normal">/ <?= number_format($khlSoll, 1, ',', '.') ?> °C</span>
                                <?php endif; ?>
                            </div>
                        </div>
                    </div>
                    <?php endif; ?>
                </div>

                <div class="row g-2 mb-3">
                    <div class="col-6 col-md-3">
                        <div class="p-2 bg-body-tertiary rounded border text-center h-100 d-flex flex-column justify-content-center">
                            <div class="small text-muted">Heizleistung<?= $heiz_kw_estimated ? ' <span class="fw-normal">(geschätzt)</span>' : '' ?></div>
                            <div class="fw-bold" title="<?= htmlspecialchars($heiz_kw_estimated ? $heiz_kw_estimate_title : '') ?>"><?= ($heiz_kw_estimated && $heiz_kw > 0 ? 'ca. ' : '') ?><?= fmtVal($heiz_kw, ' kW') ?></div>
                        </div>
                    </div>
                    <div class="col-6 col-md-3">
                        <div class="p-2 bg-body-tertiary rounded border text-center h-100 d-flex flex-column justify-content-center">
                            <div class="small text-muted">Verbrauch</div>
                            <div class="fw-bold text-warning"><?= number_format($wp_power_w / 1000, 2, ',', '.') ?> kW</div>
                        </div>
                    </div>
                    <div class="col-6 col-md-3">
                        <div class="p-2 bg-body-tertiary rounded border text-center h-100 d-flex flex-column justify-content-center">
                            <div class="small text-muted">COP</div>
                            <div class="fw-bold <?= $copColor ?>"><?= ($cop > 0) ? number_format($cop, 2, ',', '.') : '--' ?></div>
                        </div>
                    </div>
                    <div class="col-6 col-md-3">
                        <div class="p-2 bg-body-tertiary rounded border text-center h-100 d-flex flex-column justify-content-center">
                            <?php if ($wpType == 1 && $idmJazValue > 0): // IDM: Gesamt-JAZ aus Config ?>
                                <div class="small text-muted">JAZ <span class="fw-normal text-muted" style="font-size:0.7rem;">(Gesamt)</span></div>
                                <?php $idmJazColor = getCOPColor($idmJazValue, 1); ?>
                                <div class="fw-bold <?= $idmJazColor ?>" title="JAZ = <?= number_format($idmWGesamt, 0, ',', '.') ?> kWh Wärme / <?= number_format($idmETotalCfg, 0, ',', '.') ?> kWh Strom (idm_e_total in Config)">
                                    <?= number_format($idmJazValue, 2, ',', '.') ?>
                                </div>
                                <div class="small text-muted" style="font-size:0.65rem;"><?= number_format($idmWGesamt, 0, ',', '.') ?> kWh / <?= number_format($idmETotalCfg, 0, ',', '.') ?> kWh</div>
                            <?php elseif ($wpType == 1): ?>
                                <div class="small text-muted">JAZ <span class="fw-normal text-muted" style="font-size:0.7rem;">(Gesamt)</span></div>
                                <div class="text-muted small">-- </div>
                                <div class="small text-muted" style="font-size:0.65rem;">idm_e_total= in Config</div>
                            <?php else: ?>
                                <div class="small text-muted"><?= $jazLabel ?> <span title="Gesamt-AZ (JAZ)">/ JAZ</span></div>
                                <?php $jazColor = getCOPColor($jaz, $wpType); $gesamtColor = getCOPColor($gesamtJaz, $wpType); ?>
                                <div class="fw-bold <?= $jazColor ?>" title="Basierend auf Energie (Tages-Startwert) / el. Leistung vom Manager">
                                    <?= ($jaz > 0 && $jaz <= 15) ? number_format($jaz, 2, ',', '.') : '--' ?>
                                    <span class="text-muted fw-normal small <?= $gesamtColor ?>">/ <?= ($gesamtJaz > 0 && $gesamtJaz <= 15) ? number_format($gesamtJaz, 2, ',', '.') : '--' ?></span>
                                </div>
                            <?php endif; ?>
                        </div>
                    </div>
                </div>

                <div class="row g-2 mb-3">
                    <div class="col-12 col-xl-7">
                        <h6 class="text-muted text-uppercase small fw-bold mb-2">Aktueller Status</h6>
                        <div class="p-2 bg-body-tertiary rounded border d-flex flex-wrap align-items-center gap-2" style="min-height: 46px;">
                            <?php if(!empty($data['stiebel_passive_cooling_active']) || !empty($data['Passive_Kuehlung_Aktiv']) || !empty($data['Passive_Kühlung_Aktiv'])): ?>
                                <span class="badge bg-primary" title="Passive Kühlung ohne Verdichter">
                                    <i class="fas fa-snowflake me-1"></i> Passive Kühlung
                                </span>
                            <?php elseif(!empty($data['Verdichter_Ein']) || !empty($data['Verdichter'])): ?>
                                <span class="badge bg-warning text-dark" title="Verdichter läuft">
                                    <i class="fas fa-sync fa-spin me-1"></i> Verdichter an
                                    <?php if(!empty($data['Freq_Ist']) && $data['Freq_Ist'] > 0): ?>
                                        (<?= round($data['Freq_Ist']) ?> Hz)
                                    <?php endif; ?>
                                </span>
                            <?php else: ?>
                                <span class="badge bg-secondary">Standby</span>
                            <?php endif; ?>

                            <?php 
                                $baText = $data['Betriebszustand'] ?? null;
                                if (!$baText && isset($data['Betriebsart'])) {
                                    $bmMap = [0 => 'Heizen', 1 => 'Warmwasser', 2 => 'Schwimmbad', 3 => 'E-Sperre', 4 => 'Abtauen', 5 => 'Standby'];
                                    $baText = $bmMap[(int)$data['Betriebsart']] ?? null;
                                }
                                if (!$baText) $baText = '--';
                                
                                $baClass = 'bg-secondary';
                                if (strpos($baText, 'Heiz') !== false) $baClass = 'bg-warning text-dark';
                                elseif (strpos($baText, 'Warmwasser') !== false) $baClass = 'bg-danger';
                                elseif (strpos($baText, 'Kühl') !== false) $baClass = 'bg-primary';
                                elseif (strpos($baText, 'Abtau') !== false) $baClass = 'bg-info text-dark';
                            ?>
                            <span class="badge <?= $baClass ?>"><?= htmlspecialchars($baText) ?></span>
                            
                            <?php if (!empty($json['pv_pause_active'])): ?>
                                <?php
                                    $pauseOwner = (string)($json['pv_pause_owner'] ?? '');
                                    $pauseLabel = $pauseOwner === 'source_recovery_heatpump' ? 'Quell-Erholung' : 'PV-Pause';
                                    $pauseTitle = $wpType == 0
                                        ? 'Weiche Luxtronik SHI-Sollwertsperre aktiv; keine echte EVU-/SG-Ready-Sperre'
                                        : 'Wärmepumpen-Pause aktiv';
                                ?>
                                <span class="badge bg-danger" title="<?= htmlspecialchars($pauseTitle) ?>">
                                    <i class="fas fa-ban me-1"></i> <?= htmlspecialchars($pauseLabel) ?>
                                </span>
                            <?php elseif (!empty($json['boost_active'])): ?>
                                <span class="badge bg-success" title="PV-Überschuss aktiv (Smart Grid Vollgas)">
                                    <i class="fas fa-solid fa-fire-flame-curved me-1"></i> PV-Boost
                                </span>
                            <?php endif; ?>
                            
                            <?php if ($manualWwActive): ?>
                                <span class="badge bg-danger" title="Warmwasser-Timer überschrieben">
                                    <i class="fas fa-hot-tub me-1"></i> WW-Sofort Aktiv
                                </span>
                            <?php endif; ?>
                            
                            <?php
                            $wwTimerEn = (int)($conf['ww_timer_enable'] ?? 0);
                            if ($wwTimerEn == 1):
                                $wwVon = floatval($conf['wwvon'] ?? 0);
                                $wwBis = floatval($conf['wwbis'] ?? 0);
                                $circVon = floatval($conf['ww_circ_von'] ?? 0);
                                $circBis = floatval($conf['ww_circ_bis'] ?? 0);
                                
                                $nowDec = (float)date('G') + ((float)date('i') / 60.0);
                                
                                $inWw = ($wwVon <= $wwBis) ? ($nowDec >= $wwVon && $nowDec < $wwBis) : ($nowDec >= $wwVon || $nowDec < $wwBis);
                                $wwBadgeColor = $inWw ? 'bg-warning text-dark border border-warning' : 'bg-transparent text-muted border border-secondary';
                                
                                $inCirc = ($circVon <= $circBis) ? ($nowDec >= $circVon && $nowDec < $circBis) : ($nowDec >= $circVon || $nowDec < $circBis);
                                $circOnMins = (int)($conf['ww_circ_on'] ?? 0);
                                $circOffMins = (int)($conf['ww_circ_off'] ?? 0);
                                if ($inCirc && ($circOnMins + $circOffMins) > 0) {
                                    $cycle = (date('H') * 60 + date('i')) % ($circOnMins + $circOffMins);
                                    if ($cycle >= $circOnMins) $inCirc = false; // In der Pause-Taktung
                                }
                                $circBadgeColor = $inCirc ? 'bg-info text-dark border border-info' : 'bg-transparent text-muted border border-secondary';
                            ?>
                                <span class="badge <?= $wwBadgeColor ?>" title="Warmwasser-Status (Timer">
                                    <i class="fas fa-hot-tub me-1"></i> WW: <?= fmtDecTime($wwVon) ?> - <?= fmtDecTime($wwBis) ?>
                                </span>
                                <span class="badge <?= $circBadgeColor ?>" title="Zirkulations-Status (Timer, Taktung berücksichtigt)">
                                    <i class="fas fa-sync <?= $inCirc ? 'fa-spin' : '' ?> me-1"></i> Zirk: <?= fmtDecTime($circVon) ?> - <?= fmtDecTime($circBis) ?>
                                </span>
                            <?php endif; ?>
                        </div>
                    </div>
                    
                    <div class="col-12 col-xl-5">
                        <h6 class="text-muted text-uppercase small fw-bold mb-2">Wärmequelle</h6>
                        <div class="p-2 bg-body-tertiary rounded border d-flex align-items-center" style="min-height: 46px;">
                            <div class="small w-100 text-muted">
                                <?php if ($wpType == 4): ?>
                                    <?php $sourceTemp = wpFirstVal($data, ['Quellentemperatur', 'Waermequelle_Temperatur', 'Wärmequelle_Temperatur'], null); ?>
                                    <div class="d-flex justify-content-between align-items-center">
                                        <span><i class="fas fa-thermometer-half me-1"></i> ISG Wärmequelle</span>
                                        <span class="text-info"><?= fmtVal($sourceTemp, '°C') ?></span>
                                    </div>
                                <?php elseif ($wpType == 5): ?>
                                    <i class="fas fa-wind me-1"></i> Dimplex/NWPM (Außen: <?= fmtVal(wpFirstVal($data, ['Außentemperatur', 'Aussentemp'], null), '°C') ?>)
                                <?php elseif ($wpType == 1): // IDM Luft/Wasser Pumpe -> Kein Sole-Ein/Aus ?>
                                    <i class="fas fa-wind me-1"></i> Luft/Wasser (Außen: <?= fmtVal(wpFirstVal($data, ['Außentemperatur', 'Aussentemp'], '--'), '°C') ?> | Zuluft: <?= fmtVal(wpFirstVal($data, ['Zuluft'], '--'), '°C') ?>)
                                <?php else: ?>
                                    <?php
                                        $soleEin = wpFirstVal($data, ['Sole_Ein', 'Wärmequelle-Ein', 'Waermequelle_Ein', 'Waermequelle-Ein', 'WQ_Eintritt', 'Zuluft'], wpFirstVal($data, ['Außentemperatur', 'Aussentemp'], '--'));
                                        $soleAus = wpFirstVal($data, ['Sole_Aus', 'Wärmequelle-Aus', 'Waermequelle_Aus', 'Waermequelle-Aus', 'WQ_Austritt'], '--');
                                    ?>
                                    <div class="d-flex justify-content-between align-items-center">
                                        <span><i class="fas fa-water me-1"></i> Sole Wärmequelle</span>
                                        <div>
                                            <span class="text-info">Ein: <?= fmtVal($soleEin, '°C') ?></span>
                                            <span class="text-primary ms-2">Aus: <?= fmtVal($soleAus, '°C') ?></span>
                                        </div>
                                    </div>
                                <?php endif; ?>
                            </div>
                        </div>
                    </div>
                </div>

                <h6 class="text-muted text-uppercase small fw-bold mb-2"><?= $wpType == 4 ? 'Stiebel ISG Live-Werte' : ($wpType == 5 ? 'Dimplex SG-Ready' : 'Modbus Set-Werte (Vorgaben)') ?></h6>
                <?php if ($wpType == 4): ?>
                <div class="row g-2 mb-3">
                    <div class="col-6 col-md-3">
                        <div class="p-2 bg-body-tertiary rounded border text-center h-100">
                            <div class="small text-muted">SG-Ready</div>
                            <div class="fw-bold text-info">SG <?= htmlspecialchars((string)($data['stiebel_sg_ready_state'] ?? ($status['SG_State'] ?? '--'))) ?></div>
                        </div>
                    </div>
                    <div class="col-6 col-md-3">
                        <div class="p-2 bg-body-tertiary rounded border text-center h-100">
                            <div class="small text-muted">Betriebsart</div>
                            <div class="fw-bold"><?= htmlspecialchars((string)($data['stiebel_operating_mode_text'] ?? ($status['Operating_Mode_Text'] ?? '--'))) ?></div>
                            <div class="small text-muted">HK-Soll: <?= fmtVal($data['Heizkreis1_Soll'] ?? null, '°C') ?></div>
                        </div>
                    </div>
                    <div class="col-6 col-md-3">
                        <div class="p-2 bg-body-tertiary rounded border text-center h-100">
                            <div class="small text-muted">Verdichter</div>
                            <div class="fw-bold"><?= fmtVal($data['stiebel_compressor_hz'] ?? null, ' Hz') ?> <span class="text-muted small">/ <?= fmtVal($data['stiebel_compressor_percent'] ?? null, ' %') ?></span></div>
                        </div>
                    </div>
                    <div class="col-6 col-md-3">
                        <div class="p-2 bg-body-tertiary rounded border text-center h-100">
                            <div class="small text-muted">Leistungsquelle</div>
                            <?php
                                $stiebelPowerSourceRaw = (string)($data['stiebel_power_source'] ?? '--');
                                $stiebelPowerSourceLabel = $stiebelPowerSourceRaw;
                                if ($stiebelPowerSourceRaw === 'status_nominal_dhw') $stiebelPowerSourceLabel = 'WW';
                                elseif ($stiebelPowerSourceRaw === 'status_nominal_heating') $stiebelPowerSourceLabel = 'Heizen';
                                elseif ($stiebelPowerSourceRaw === 'passive_cooling_standby') $stiebelPowerSourceLabel = 'Passive Kühlung';
                                elseif ($stiebelPowerSourceRaw === 'standby') $stiebelPowerSourceLabel = 'Standby';
                            ?>
                            <div class="fw-bold small" title="<?= htmlspecialchars($stiebelPowerSourceRaw) ?>"><?= htmlspecialchars($stiebelPowerSourceLabel) ?></div>
                        </div>
                    </div>
                </div>
                <?php elseif ($wpType == 5): ?>
                <?php
                    $dimplexSgLive = $data['dimplex_sg_value'] ?? ($status['SG_Value'] ?? null);
                    $dimplexSgSet = $json['dimplex_sg_state'] ?? null;
                    $dimplexSgColor = $data['dimplex_sg_color'] ?? ($status['SG_Color'] ?? '--');
                    $dimplexSgText = $data['dimplex_sg_state'] ?? ($status['SG_State'] ?? '--');
                    $dimplexOperatingMode = $data['dimplex_operating_mode_text'] ?? ($data['Betriebsmodus'] ?? '--');
                    $dimplexHeatPowerW = $data['dimplex_heat_power_w'] ?? null;
                    $dimplexElectricPowerW = $data['dimplex_electric_power_w'] ?? ($data['Leistung_Verdichter_W'] ?? null);
                    $dimplexHeatPowerEstimated = !empty($data['dimplex_heat_power_estimated']);
                    $dimplexWpmSoftware = $data['dimplex_wpm_software'] ?? ($json['dimplex']['wpm_software'] ?? '--');
                    $dimplexPowerNote = $data['dimplex_power_note'] ?? null;
                    $dimplexSgNote = $data['dimplex_sg_note'] ?? null;
                    $dimplexDiagNotes = array_filter([$dimplexSgNote, $dimplexPowerNote], fn($v) => is_string($v) && trim($v) !== '');
                ?>
                <div class="row g-2 mb-3">
                    <div class="col-6 col-md-3">
                        <div class="p-2 bg-body-tertiary rounded border text-center h-100">
                            <div class="small text-muted">SG Live</div>
                            <div class="fw-bold text-info"><?= htmlspecialchars((string)($dimplexSgLive ?? '--')) ?> · <?= htmlspecialchars((string)$dimplexSgColor) ?></div>
                        </div>
                    </div>
                    <div class="col-6 col-md-3">
                        <div class="p-2 bg-body-tertiary rounded border text-center h-100">
                            <div class="small text-muted">Status</div>
                            <div class="fw-bold small"><?= htmlspecialchars((string)$dimplexSgText) ?></div>
                        </div>
                    </div>
                    <div class="col-6 col-md-3">
                        <div class="p-2 bg-body-tertiary rounded border text-center h-100">
                            <div class="small text-muted">Betriebsmodus</div>
                            <div class="fw-bold small"><?= htmlspecialchars((string)$dimplexOperatingMode) ?></div>
                        </div>
                    </div>
                    <div class="col-6 col-md-3">
                        <div class="p-2 bg-body-tertiary rounded border text-center h-100">
                            <div class="small text-muted">Elektr. Leistung</div>
                            <div class="fw-bold"><?= fmtVal($dimplexElectricPowerW, ' W') ?></div>
                            <div class="text-muted small">Roh <?= htmlspecialchars((string)($data['dimplex_electric_power_raw'] ?? '--')) ?> / PDU <?= htmlspecialchars((string)($data['dimplex_electric_power_address'] ?? '--')) ?></div>
                        </div>
                    </div>
                    <div class="col-6 col-md-3">
                        <div class="p-2 bg-body-tertiary rounded border text-center h-100">
                            <div class="small text-muted">Wärmeleistung<?= $dimplexHeatPowerEstimated ? ' <span class="fw-normal">(geschätzt)</span>' : '' ?></div>
                            <div class="fw-bold"><?= $dimplexHeatPowerEstimated ? 'ca. ' : '' ?><?= fmtVal($dimplexHeatPowerW, ' W') ?></div>
                            <div class="text-muted small">Roh <?= htmlspecialchars((string)($data['dimplex_heat_power_raw'] ?? '--')) ?> / PDU <?= htmlspecialchars((string)($data['dimplex_heat_power_address'] ?? '--')) ?></div>
                        </div>
                    </div>
                    <div class="col-6 col-md-3">
                        <div class="p-2 bg-body-tertiary rounded border text-center h-100">
                            <div class="small text-muted">Letzter Sollwert</div>
                            <div class="fw-bold"><?= htmlspecialchars((string)($dimplexSgSet ?? '--')) ?></div>
                        </div>
                    </div>
                    <div class="col-6 col-md-3">
                        <div class="p-2 bg-body-tertiary rounded border text-center h-100">
                            <div class="small text-muted">Register</div>
                            <div class="fw-bold small"><?= htmlspecialchars((string)($json['dimplex_sg_register'] ?? ($data['dimplex_sg_register'] ?? '5167'))) ?> / PDU <?= htmlspecialchars((string)($json['dimplex_sg_address'] ?? ($data['dimplex_sg_address'] ?? '5166'))) ?></div>
                        </div>
                    </div>
                    <div class="col-6 col-md-3">
                        <div class="p-2 bg-body-tertiary rounded border text-center h-100">
                            <div class="small text-muted">WPM Software</div>
                            <div class="fw-bold small"><?= htmlspecialchars((string)($dimplexWpmSoftware ?: '--')) ?></div>
                        </div>
                    </div>
                </div>
                <?php if (!empty($dimplexDiagNotes)): ?>
                <div class="alert alert-warning py-2 px-3 small">
                    <?= htmlspecialchars(implode(' ', $dimplexDiagNotes)) ?>
                </div>
                <?php endif; ?>
                <?php elseif ($wpType == 1): // IDM Layout ?>
                <?php
                    $extHz  = intval($json['idm_ext_hz']  ?? 0);
                    $extWw  = intval($json['idm_ext_ww']  ?? 0);
                    $extKhl = intval($json['idm_ext_khl'] ?? 0);
                    $idmCoolingOrigin = (string)($json['idm_cooling_origin'] ?? 'off');
                    $idmCoolingOriginLabel = 'Keine Kühlanforderung';
                    if ($idmCoolingOrigin === 'internal_control') $idmCoolingOriginLabel = 'iDM intern';
                    elseif ($idmCoolingOrigin === 'external_request') $idmCoolingOriginLabel = 'EMS extern';
                ?>
                <div class="row g-2 mb-3">
                    <div class="col-4">
                        <div class="p-2 bg-body-tertiary rounded border text-center <?= $extHz ? 'border-danger border-opacity-50' : '' ?>">
                            <div class="small text-muted mb-1"><i class="fas fa-fire me-1"></i>Heizung</div>
                            <span class="badge <?= $extHz  ? 'bg-danger'  : 'bg-secondary opacity-50' ?>" title="Externe Heizanforderung (Register 1710)">HZ=<?= $extHz ?></span>
                        </div>
                    </div>
                    <div class="col-4">
                        <div class="p-2 bg-body-tertiary rounded border text-center <?= $extWw ? 'border-warning border-opacity-50' : '' ?>">
                            <div class="small text-muted mb-1"><i class="fas fa-tint me-1"></i>Warmwasser</div>
                            <span class="badge <?= $extWw  ? 'bg-warning text-dark' : 'bg-secondary opacity-50' ?>" title="Externe WW-Anforderung (Register 1712)">WW=<?= $extWw ?></span>
                        </div>
                    </div>
                    <div class="col-4">
                        <div class="p-2 bg-body-tertiary rounded border text-center <?= $extKhl ? 'border-primary border-opacity-50' : '' ?>">
                            <div class="small text-muted mb-1"><i class="fas fa-snowflake me-1"></i>Kühlung</div>
                            <span class="badge <?= $extKhl ? 'bg-primary' : 'bg-secondary opacity-50' ?>" title="Externe Kühlanforderung (Register 1711)">KHL=<?= $extKhl ?></span>
                            <div class="small text-muted mt-1" title="Herkunft der aktuellen iDM-Kühlung bzw. Kühlanforderung"><?= htmlspecialchars($idmCoolingOriginLabel) ?></div>
                        </div>
                    </div>
                </div>
                <?php else: // Luxtronik Layout ?>
                <div class="row g-2 mb-3">
                    <div class="col-6">
                        <div class="p-2 bg-body-tertiary rounded border text-center">
                            <div class="small text-muted">Heizung (Modus / Soll)</div>
                            <div class="fw-bold text-danger"><?= htmlspecialchars($data['Modus Heizen'] ?? '--') ?> / <?= fmtVal($data['Sollwert Heizen'] ?? ($data['Heizung_Temperatur_Soll'] ?? '--'), ' °C') ?></div>
                        </div>
                    </div>
                    <div class="col-6">
                        <div class="p-2 bg-body-tertiary rounded border text-center">
                            <div class="small text-muted">Warmwasser (Modus / Soll)</div>
                            <div class="fw-bold text-warning"><?= htmlspecialchars($data['Modus Warmw.'] ?? '--') ?> / <?= fmtVal($data['Sollwert Warmw.'] ?? ($data['Warmwasser_Temperatur_Soll'] ?? '--'), ' °C') ?></div>
                        </div>
                    </div>
                </div>
                <?php endif; ?>
        <?php endif; ?>
        <?php endif; // isChargingOnly / wpType2 / WP ?>

        <div class="d-flex justify-content-between align-items-center mt-3 pt-2 border-top">
            <span class="small text-muted"><i class="fas fa-robot me-1"></i> <?= htmlspecialchars($displayServiceLabel) ?>: <span class="badge <?= $displayServiceRunning ? 'bg-success' : 'bg-danger' ?>"><?= strtoupper($displayServiceStatus) ?></span></span>
            <div class="d-flex gap-2">
                <a href="<?= getContextPageUrl('config', ['expand' => 'luxtronik']) ?>#group-luxtronik" class="btn btn-sm btn-outline-secondary"><i class="fas fa-cog"></i></a>
                <form method="post"><button type="submit" name="restart_manager" class="btn btn-sm btn-outline-info"><i class="fas fa-sync-alt"></i></button></form>
            </div>
        </div>
    </div>
</div>



<?= renderE3dcModalThemeStyles() ?>
<!-- Energy Manager Log Modal -->
<div class="modal fade" id="energyManagerLogModal" tabindex="-1" aria-hidden="true">
    <div class="modal-dialog modal-lg modal-dialog-scrollable">
        <div class="modal-content bg-body-secondary text-body border-secondary">
            <div class="modal-header border-secondary">
                <h5 class="modal-title"><i class="fas fa-robot me-2"></i>Energy Manager Log</h5>
                <button type="button" class="btn-close e3dc-modal-close" data-bs-dismiss="modal" aria-label="Close"></button>
            </div>
            <div class="modal-body e3dc-log-body p-2">
                <pre id="energy-manager-log-content" class="e3dc-log-pre">Lade Protokoll...</pre>
            </div>
            <div class="modal-footer border-secondary">
                <button type="button" class="btn btn-secondary w-100" data-bs-dismiss="modal">Schließen</button>
            </div>
        </div>
    </div>
</div>

<script>
var isLuxtronikEditing = false;


// Funktion zum automatischen Neuladen der Daten
function refreshLuxtronik() {
    // Nicht aktualisieren, wenn User gerade editiert
    if (typeof isLuxtronikEditing !== 'undefined' && isLuxtronikEditing) return;

    // Wir suchen das Element mit der ID 'luxtronik-card'.
    const container = document.getElementById('luxtronik-card');
    if (!container) return;

    // Wir laden einfach den Inhalt der aktuellen Seite neu und extrahieren die Karte
    let url = new URL(window.location.href);
    url.searchParams.set('t', Date.now());

    fetch(url)
        .then(response => response.text())
        .then(html => {
            const parser = new DOMParser();
            const doc = parser.parseFromString(html, 'text/html');
            const newContent = doc.getElementById('luxtronik-card').innerHTML;
            container.innerHTML = newContent;
            console.log("Luxtronik Daten aktualisiert: " + new Date().toLocaleTimeString());
        })
        .catch(err => console.warn('Update fehlgeschlagen:', err));
}

function showEnergyManagerLog() {
    const modal = new bootstrap.Modal(document.getElementById('energyManagerLogModal'));
    modal.show();
    document.getElementById('energy-manager-log-content').innerText = 'Lade Protokoll...';
    
    // Die Action wird von index.php/mobile.php verarbeitet, je nachdem, wo waermepumpe.php inkludiert ist.
    // Wir nutzen einen relativen Pfad, der im aktuellen Kontext funktioniert.
    fetch('?action=get_energy_manager_log')
        .then(r => r.text())
        .then(text => {
            // Zeilen umkehren, damit die neuesten Einträge oben stehen
            // (Split am Zeilenumbruch -> Array umdrehen -> Join)
            const reversedText = text.trim().split('\n').reverse().join('\n');
            document.getElementById('energy-manager-log-content').innerText = reversedText;
        })
        .catch(e => {
            document.getElementById('energy-manager-log-content').innerText = 'Fehler beim Laden des Protokolls.';
        });
}

// Alle 15 Sekunden aktualisieren
setInterval(refreshLuxtronik, 2000);
</script>
