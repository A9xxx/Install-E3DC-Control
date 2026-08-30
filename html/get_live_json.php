<?php
// get_live_json.php
require_once 'helpers.php';

function liveCloseSessionLock() {
    if (session_status() === PHP_SESSION_ACTIVE) {
        @session_write_close();
    }
}

function liveJsonSuccessfulSnapshotBody($body) {
    if (!is_string($body) || $body === '') return null;
    $decoded = @json_decode($body, true);
    if (
        !is_array($decoded)
        || array_key_exists('error', $decoded)
        || (array_key_exists('success', $decoded) && $decoded['success'] === false)
    ) {
        return null;
    }
    return $decoded;
}

function liveJsonReadCachedBody($path, $maxAgeS) {
    if (is_link($path)) return null;
    clearstatcache(true, $path);
    $meta = @lstat($path);
    if (
        !is_array($meta)
        || (((int)($meta['mode'] ?? 0)) & 0170000) !== 0100000
        || (int)($meta['nlink'] ?? 0) !== 1
        || (int)($meta['size'] ?? 0) <= 0
        || (int)($meta['size'] ?? 0) > 16 * 1024 * 1024
        || !is_readable($path)
    ) {
        return null;
    }
    $age = microtime(true) - (float)($meta['mtime'] ?? 0);
    if ($age < 0.0 || $age > max(0.0, (float)$maxAgeS)) return null;
    $body = e3dcReadRegularFileBound($path, 16 * 1024 * 1024);
    clearstatcache(true, $path);
    $after = @lstat($path);
    if (
        !is_array($after)
        || (int)($after['dev'] ?? -1) !== (int)($meta['dev'] ?? -2)
        || (int)($after['ino'] ?? -1) !== (int)($meta['ino'] ?? -2)
        || (int)($after['size'] ?? -1) !== (int)($meta['size'] ?? -2)
        || (int)($after['mtime'] ?? -1) !== (int)($meta['mtime'] ?? -2)
        || (int)($after['ctime'] ?? -1) !== (int)($meta['ctime'] ?? -2)
    ) {
        return null;
    }
    if (liveJsonSuccessfulSnapshotBody($body) === null) return null;
    return ['body' => $body, 'age_s' => $age];
}

function liveJsonWriteRuntimeFileAtomic($path, $body) {
    $dir = dirname((string)$path);
    if (!is_dir($dir) || !is_string($body)) return false;
    $tmp = @tempnam($dir, '.get_live_json_');
    if ($tmp === false) return false;
    $ok = @file_put_contents($tmp, $body, LOCK_EX) !== false;
    if ($ok) {
        @chmod($tmp, 0664);
        $ok = @rename($tmp, $path);
    }
    @unlink($tmp);
    if ($ok) @chmod($path, 0664);
    return $ok;
}

function liveJsonOpenBoundLockFile($path) {
    if (is_link($path)) return false;
    $handle = @fopen($path, 'c');
    if ($handle === false) {
        $handle = @fopen($path, 'r');
    }
    if ($handle === false) return false;
    @chmod($path, 0664);
    clearstatcache(true, $path);
    $opened = @fstat($handle);
    $bound = @lstat($path);
    if (
        !is_array($opened)
        || !is_array($bound)
        || (((int)($opened['mode'] ?? 0)) & 0170000) !== 0100000
        || (((int)($bound['mode'] ?? 0)) & 0170000) !== 0100000
        || (int)($opened['nlink'] ?? 0) !== 1
        || (int)($bound['nlink'] ?? 0) !== 1
        || (int)($opened['dev'] ?? -1) !== (int)($bound['dev'] ?? -2)
        || (int)($opened['ino'] ?? -1) !== (int)($bound['ino'] ?? -2)
    ) {
        @fclose($handle);
        return false;
    }
    return $handle;
}

function liveJsonTryExclusiveLock($handle, $waitMs) {
    if (!is_resource($handle)) return false;
    $deadline = microtime(true) + max(0.0, (float)$waitMs) / 1000.0;
    do {
        if (@flock($handle, LOCK_EX | LOCK_NB)) return true;
        if (microtime(true) >= $deadline) break;
        usleep(25000);
    } while (true);
    return false;
}

function liveJsonReleaseLock($handle, $owned = true) {
    if (!is_resource($handle)) return;
    if ($owned) @flock($handle, LOCK_UN);
    @fclose($handle);
}

function liveJsonEmitError($status, $error) {
    http_response_code((int)$status);
    echo json_encode([
        'success' => false,
        'error' => (string)$error,
    ], JSON_UNESCAPED_SLASHES);
}

$liveJsonIsCli = (PHP_SAPI === 'cli');
$liveJsonCliHistoryRequested = false;
$liveJsonCliHistoryLockHandle = null;
$liveJsonCliHistoryLockOwned = false;
$liveJsonCliThrottleFile = '/var/www/html/ramdisk/get_live_json_history_sample.ts';

if ($liveJsonIsCli) {
    $cliArgs = isset($argv) && is_array($argv) ? array_slice($argv, 1) : [];
    $liveJsonCliHistoryRequested = ($cliArgs === ['--history-sample']);
    if (!$liveJsonCliHistoryRequested) {
        if (defined('STDERR')) {
            @fwrite(STDERR, "Nur --history-sample ist als CLI-Vertrag zulässig.\n");
        }
        exit(64);
    }
    liveCloseSessionLock();
    $liveJsonCliHistoryLockHandle = liveJsonOpenBoundLockFile(
        '/var/www/html/ramdisk/get_live_json_history_sample.lock'
    );
    if (!is_resource($liveJsonCliHistoryLockHandle)) {
        exit(75);
    }
    $liveJsonCliHistoryLockOwned = @flock(
        $liveJsonCliHistoryLockHandle,
        LOCK_EX | LOCK_NB
    );
    if (!$liveJsonCliHistoryLockOwned) {
        liveJsonReleaseLock($liveJsonCliHistoryLockHandle, false);
        exit(0);
    }
    $lastHistoryRaw = e3dcReadRegularFileBound($liveJsonCliThrottleFile, 128);
    $lastHistorySample = is_string($lastHistoryRaw) && is_numeric(trim($lastHistoryRaw))
        ? (float)trim($lastHistoryRaw)
        : 0.0;
    $lastHistorySampleAge = microtime(true) - $lastHistorySample;
    if (
        $lastHistorySample > 0.0
        && $lastHistorySampleAge >= 0.0
        && $lastHistorySampleAge < 58.0
    ) {
        liveJsonReleaseLock($liveJsonCliHistoryLockHandle, true);
        exit(0);
    }
} else {
    requireWebAuth(true);
    header('Content-Type: application/json');
    header('Cache-Control: no-cache, no-store, must-revalidate');
    header('Pragma: no-cache');
    header('Expires: 0');
}

if (
    !$liveJsonIsCli
    && ($_SERVER['REQUEST_METHOD'] ?? 'GET') === 'GET'
    && isset($_GET['wallbox_native_snapshot'])
    && (string)$_GET['wallbox_native_snapshot'] === '1'
) {
    liveCloseSessionLock();
    $snapshotPath = '/var/www/html/ramdisk/wallbox_native.json';
    if (
        is_link($snapshotPath)
        || !is_file($snapshotPath)
        || !is_readable($snapshotPath)
        || (int)@filesize($snapshotPath) > 1024 * 1024
    ) {
        http_response_code(404);
        echo json_encode(['success' => false, 'error' => 'wallbox_snapshot_unavailable']);
        exit;
    }
    $snapshotRaw = @file_get_contents($snapshotPath);
    $snapshot = is_string($snapshotRaw) ? json_decode($snapshotRaw, true) : null;
    if (!is_array($snapshot)) {
        http_response_code(503);
        echo json_encode(['success' => false, 'error' => 'wallbox_snapshot_invalid']);
        exit;
    }
    $snapshotConfigData = loadE3dcConfig();
    $snapshotConfig = $snapshotConfigData['config'] ?? [];
    $snapshotWb1Configured = hasWallbox1Config($snapshotConfig);
    $snapshotWb2Disabled = isWallbox2ExplicitlyDisabledConfig($snapshotConfig);
    $snapshotWb2Configured = !$snapshotWb2Disabled && (
        hasWallbox2ExplicitConfig($snapshotConfig)
        || e3dcWallbox2RuntimeEvidence($snapshot)
    );
    e3dcApplyWallboxPresenceProjection(
        $snapshot,
        $snapshotWb1Configured,
        $snapshotWb2Configured,
        $snapshotWb2Disabled
    );
    $snapshot['wb_configured'] = $snapshotWb1Configured;
    $snapshot['wb2_configured'] = $snapshotWb2Configured;
    if ($snapshotWb2Configured
        && !hasWallbox2ExplicitConfig($snapshotConfig)) {
        $snapshot['wb2_native_type'] = 'openwb';
        $snapshot['is_external_wb2'] = true;
    }
    echo json_encode($snapshot, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    exit;
}

// Die Ladekurve benötigt aus der Live-Historie ausschließlich Zeit und SoC.
// Dieser eng begrenzte Lesepfad antwortet vor dem vollständigen Live-Snapshot:
// keine Geräteabfragen, keine Historienpflege und kein unnötig großer Transfer.
if (
    !$liveJsonIsCli
    && ($_SERVER['REQUEST_METHOD'] ?? 'GET') === 'GET'
    && isset($_GET['storage_curve_history'])
    && (string)$_GET['storage_curve_history'] === '1'
) {
    liveCloseSessionLock();
    $dayStartMsRaw = $_GET['day_start_ms'] ?? null;
    if (!is_numeric($dayStartMsRaw)) {
        http_response_code(400);
        echo json_encode([
            'success' => false,
            'error' => 'invalid_day_start',
            'points' => [],
        ], JSON_UNESCAPED_SLASHES);
        exit;
    }

    $dayStartMs = (int)round((float)$dayStartMsRaw);
    $dayStartS = (int)floor($dayStartMs / 1000);
    // Der nächste lokale Kalendertag kann bei einer Zeitumstellung 23 oder
    // 25 Stunden entfernt liegen. Ein starres 24-h-Fenster würde dabei
    // Historienpunkte des falschen Tages ein- oder ausschließen.
    $dayEndS = strtotime('+1 day', $dayStartS);
    if ($dayEndS === false || $dayEndS <= $dayStartS) {
        http_response_code(400);
        echo json_encode([
            'success' => false,
            'error' => 'invalid_day_range',
            'points' => [],
        ], JSON_UNESCAPED_SLASHES);
        exit;
    }
    $nowS = time();
    if ($dayStartS <= 0 || $dayStartS < ($nowS - 7 * 86400) || $dayStartS > ($nowS + 2 * 86400)) {
        http_response_code(400);
        echo json_encode([
            'success' => false,
            'error' => 'day_start_out_of_range',
            'points' => [],
        ], JSON_UNESCAPED_SLASHES);
        exit;
    }

    $historyFile = '/var/www/html/ramdisk/live_history.txt';
    $points = [];
    $scannedLines = 0;
    if (is_readable($historyFile)) {
        $history = new SplFileObject($historyFile, 'r');
        while (!$history->eof() && $scannedLines < 10000) {
            $line = trim((string)$history->fgets());
            $scannedLines++;
            if ($line === '') continue;
            $sample = json_decode($line, true);
            if (!is_array($sample) || !isset($sample['ts']) || !isset($sample['soc']) || !is_numeric($sample['soc'])) {
                continue;
            }
            $sampleTs = strtotime((string)$sample['ts']);
            if ($sampleTs === false || $sampleTs < $dayStartS || $sampleTs >= $dayEndS) {
                continue;
            }
            $soc = (float)$sample['soc'];
            if (!is_finite($soc) || $soc < 0.0 || $soc > 100.0) {
                continue;
            }
            $points[] = [
                'ts' => $sampleTs * 1000,
                'soc' => round($soc, 2),
            ];
        }
    }

    header('X-E3DC-History-Samples: ' . count($points));
    echo json_encode([
        'success' => true,
        'day_start_ms' => $dayStartS * 1000,
        'points' => $points,
    ], JSON_UNESCAPED_SLASHES);
    exit;
}

// API-/Widget-Kompatibilität erhält ausschließlich den bereits erzeugten,
// vollständigen Web-Snapshot. Dieser Pfad führt keinerlei Geräteabfrage oder
// Pflege aus. Ein fehlender oder veralteter Vollsnapshot bleibt explizit
// unavailable; das rohe live_data_py-Schema ist kein kompatibler Ersatz.
if (
    !$liveJsonIsCli
    && ($_SERVER['REQUEST_METHOD'] ?? 'GET') === 'GET'
    && (
        (isset($_GET['read_only_snapshot']) && (string)$_GET['read_only_snapshot'] === '1')
        || isset($_GET['t'])
        || isset($_GET['cache_bust'])
        || empty($_GET)
    )
) {
    liveCloseSessionLock();
    $readOnlySnapshot = liveJsonReadCachedBody(
        '/var/www/html/ramdisk/get_live_json_snapshot.json',
        15.0
    );
    if (!is_array($readOnlySnapshot)) {
        liveJsonEmitError(503, 'read_only_snapshot_unavailable');
        exit;
    }
    header('X-E3DC-Live-Cache: READ_ONLY');
    header('X-E3DC-Live-Cache-Age: ' . number_format(
        (float)$readOnlySnapshot['age_s'],
        3,
        '.',
        ''
    ));
    echo $readOnlySnapshot['body'];
    exit;
}

// Der vollständige Live-Sampler pflegt Caches, Historien und
// Wallbox-Sitzungszustände. Er ist daher kein sicherer GET-Endpunkt.
// Ausschließlich die oben gebundenen, eng begrenzten Lesepfade bleiben read-only.
if (!$liveJsonIsCli) {
    e3dcRequirePostMutation(true);
    liveCloseSessionLock();
}

function liveJsonShortCacheAllowed() {
    if (PHP_SAPI === 'cli') return true;
    if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'POST') return false;
    if (isset($_GET['cache']) && (string)$_GET['cache'] === '0') return false;
    $ignored = ['t' => true, '_' => true, 'cache_bust' => true];
    foreach ($_GET as $key => $_value) {
        if (!isset($ignored[(string)$key])) return false;
    }
    return true;
}

$liveJsonShortCacheEnabled = liveJsonShortCacheAllowed();
$liveJsonShortCacheFile = '/var/www/html/ramdisk/get_live_json_snapshot.json';
$liveJsonShortCacheTtlS = 1.0;

if ($liveJsonShortCacheEnabled) {
    $cached = liveJsonReadCachedBody($liveJsonShortCacheFile, $liveJsonShortCacheTtlS);
    if (is_array($cached)) {
        if (!$liveJsonIsCli) header('X-E3DC-Live-Cache: HIT');
        echo $cached['body'];
        if ($liveJsonCliHistoryLockOwned) {
            liveJsonReleaseLock($liveJsonCliHistoryLockHandle, true);
        }
        exit;
    }
}

$liveJsonLockHandle = liveJsonOpenBoundLockFile(
    '/var/www/html/ramdisk/get_live_json_snapshot.lock'
);
$liveJsonLockOwned = false;
if (!is_resource($liveJsonLockHandle)) {
    if ($liveJsonCliHistoryLockOwned) {
        liveJsonReleaseLock($liveJsonCliHistoryLockHandle, true);
    }
    if ($liveJsonIsCli) exit(75);
    liveJsonEmitError(503, 'live_snapshot_lock_unavailable');
    exit;
}

$liveJsonLockOwned = @flock($liveJsonLockHandle, LOCK_EX | LOCK_NB);
if (!$liveJsonLockOwned) {
    if ($liveJsonIsCli) {
        liveJsonReleaseLock($liveJsonLockHandle, false);
        liveJsonReleaseLock($liveJsonCliHistoryLockHandle, true);
        exit(0);
    }
    if ($liveJsonShortCacheEnabled) {
        $stale = liveJsonReadCachedBody($liveJsonShortCacheFile, 15.0);
        if (is_array($stale)) {
            liveJsonReleaseLock($liveJsonLockHandle, false);
            header('X-E3DC-Live-Cache: STALE_BUSY');
            header('X-E3DC-Live-Cache-Age: ' . number_format(
                (float)$stale['age_s'],
                3,
                '.',
                ''
            ));
            echo $stale['body'];
            exit;
        }
    }
    $liveJsonLockOwned = liveJsonTryExclusiveLock($liveJsonLockHandle, 750);
}

if (!$liveJsonLockOwned) {
    liveJsonReleaseLock($liveJsonLockHandle, false);
    if ($liveJsonShortCacheEnabled) {
        $stale = liveJsonReadCachedBody($liveJsonShortCacheFile, 15.0);
        if (is_array($stale)) {
            header('X-E3DC-Live-Cache: STALE_AFTER_WAIT');
            echo $stale['body'];
            exit;
        }
    }
    liveJsonEmitError(503, 'live_snapshot_busy');
    exit;
}

if ($liveJsonShortCacheEnabled) {
    // Ein anderer Worker kann den Snapshot während unserer kurzen Wartezeit
    // bereits fertiggestellt haben.
    $cached = liveJsonReadCachedBody($liveJsonShortCacheFile, $liveJsonShortCacheTtlS);
    if (is_array($cached)) {
        liveJsonReleaseLock($liveJsonLockHandle, true);
        if (!$liveJsonIsCli) header('X-E3DC-Live-Cache: HIT_AFTER_WAIT');
        echo $cached['body'];
        if ($liveJsonCliHistoryLockOwned) {
            liveJsonReleaseLock($liveJsonCliHistoryLockHandle, true);
        }
        exit;
    }
}

if ($liveJsonCliHistoryRequested) {
    $historyThrottleWritten = liveJsonWriteRuntimeFileAtomic(
        $liveJsonCliThrottleFile,
        sprintf('%.6f', microtime(true))
    );
    if (!$historyThrottleWritten) {
        liveJsonReleaseLock($liveJsonLockHandle, true);
        liveJsonReleaseLock($liveJsonCliHistoryLockHandle, true);
        exit(75);
    }
}

ob_start();
register_shutdown_function(function () use (
        $liveJsonShortCacheEnabled,
        $liveJsonShortCacheFile,
        $liveJsonLockHandle,
        $liveJsonLockOwned,
        $liveJsonCliHistoryLockHandle,
        $liveJsonCliHistoryLockOwned
    ) {
        $body = ob_get_contents();
        $status = (int)http_response_code();
        if ($status <= 0) $status = 200;
        if (
            $liveJsonShortCacheEnabled
            && $status >= 200
            && $status < 300
            && liveJsonSuccessfulSnapshotBody($body) !== null
        ) {
            liveJsonWriteRuntimeFileAtomic($liveJsonShortCacheFile, $body);
        }
        liveJsonReleaseLock($liveJsonLockHandle, $liveJsonLockOwned);
        if ($liveJsonCliHistoryLockOwned) {
            liveJsonReleaseLock(
                $liveJsonCliHistoryLockHandle,
                true
            );
        }
    });

function liveOptionalFloatValue($value, $precision = 2) {
    if ($value === null || $value === '' || !is_numeric($value)) return null;
    return round((float)$value, (int)$precision);
}

function liveReadbackAgeSeconds($timestamp, $nowTs = null) {
    if ($timestamp === null || $timestamp === '') return null;
    if (is_numeric($timestamp)) {
        $readbackTs = (float)$timestamp;
        if ($readbackTs > 20000000000.0) $readbackTs /= 1000.0;
    } else {
        $parsed = strtotime((string)$timestamp);
        if ($parsed === false) return null;
        $readbackTs = (float)$parsed;
    }
    if (!is_finite($readbackTs) || $readbackTs <= 0.0) return null;
    $now = $nowTs === null ? microtime(true) : (float)$nowTs;
    if (($readbackTs - $now) > 5.0) return null;
    return max(0.0, $now - $readbackTs);
}

function liveSgReadyReadbackProjection($source, $nowTs = null, $maxAgeS = 45.0) {
    $projection = [
        'wp_sg_ready_active' => null,
        'wp_sg_ready_valid' => false,
        'wp_sg_ready_state' => 'unavailable',
        'wp_sg_ready_source' => 'unavailable',
        'wp_sg_ready_label' => '',
        'wp_sg_ready_age_s' => null,
    ];
    if (!is_array($source)) return $projection;

    $nested = isset($source['data']) && is_array($source['data'])
        ? $source['data']
        : [];
    $maxAge = max(1.0, (float)$maxAgeS);

    $shellyState = $source['shelly_sg_readback_state'] ?? null;
    $shellyReadbackSource = (string)($source['shelly_sg_readback_source'] ?? '');
    $shellyAgeS = liveReadbackAgeSeconds(
        $source['shelly_sg_readback_ts'] ?? null,
        $nowTs
    );
    $shellyValid = is_bool($shellyState)
        && ($source['shelly_sg_readback_confirmed'] ?? false) === true
        && $shellyReadbackSource === 'shelly_relay_confirmed_readback'
        && $shellyAgeS !== null
        && $shellyAgeS <= $maxAge;
    if ($shellyValid) {
        return [
            'wp_sg_ready_active' => $shellyState,
            'wp_sg_ready_valid' => true,
            'wp_sg_ready_state' => $shellyState ? 'boost' : 'normal',
            'wp_sg_ready_source' => $shellyReadbackSource,
            'wp_sg_ready_label' => $shellyState ? 'Shelly Boost aktiv' : 'SG-Ready Normalbetrieb',
            'wp_sg_ready_age_s' => round($shellyAgeS, 1),
        ];
    }

    $dimplexRaw = $source['dimplex_sg_readback_state']
        ?? $nested['dimplex_sg_readback_state']
        ?? null;
    $dimplexReadbackTs = $source['dimplex_sg_readback_ts']
        ?? $nested['dimplex_sg_readback_ts']
        ?? null;
    $dimplexReadbackSource = (string)(
        $source['dimplex_sg_readback_source']
        ?? $nested['dimplex_sg_readback_source']
        ?? ''
    );
    $dimplexConfirmed = (
        $source['dimplex_sg_readback_confirmed']
        ?? $nested['dimplex_sg_readback_confirmed']
        ?? false
    ) === true;
    $dimplexAgeS = liveReadbackAgeSeconds($dimplexReadbackTs, $nowTs);
    $dimplexState = is_numeric($dimplexRaw) ? (int)$dimplexRaw : null;
    $dimplexLabels = [
        0 => 'SG-Ready Normalbetrieb',
        10 => 'SG-Ready Normalbetrieb',
        11 => 'SG-Ready Anhebung aktiv',
        12 => 'SG-Ready EVU-Sperre',
        13 => 'SG-Ready maximale Anhebung',
    ];
    $dimplexValid = $dimplexState !== null
        && array_key_exists($dimplexState, $dimplexLabels)
        && $dimplexConfirmed
        && in_array($dimplexReadbackSource, [
            'dimplex_modbus_live_readback',
            'dimplex_modbus_confirmed_readback',
        ], true)
        && $dimplexAgeS !== null
        && $dimplexAgeS <= $maxAge;
    if (!$dimplexValid) return $projection;

    $active = in_array($dimplexState, [11, 13], true);
    return [
        'wp_sg_ready_active' => $active,
        'wp_sg_ready_valid' => true,
        'wp_sg_ready_state' => $dimplexState === 12
            ? 'blocked'
            : ($active ? 'boost' : 'normal'),
        'wp_sg_ready_source' => $dimplexReadbackSource,
        'wp_sg_ready_label' => $dimplexLabels[$dimplexState],
        'wp_sg_ready_age_s' => round($dimplexAgeS, 1),
    ];
}

function fetchShellyPower($ip) {
    if (empty($ip) || $ip === '0.0.0.0') return false;

    // RAM-Disk Cache gegen PHP-Worker Exhaustion bei Offline-Shellys
    $offlineFlag = '/var/www/html/ramdisk/shelly_offline_' . md5($ip) . '.flag';
    if (file_exists($offlineFlag) && (time() - filemtime($offlineFlag) < 15)) {
        return false; // Kontaktversuch für 15 Sekunden aussetzen
    }

    // Kurzer Timeout für Live-UI! 1.5s reicht im lokalen LAN (0.5s zu aggressiv für manche Shellys)
    $ctx = stream_context_create(['http' => ['timeout' => 1.5]]);

    $json = @file_get_contents("http://$ip/status", false, $ctx);
    if ($json !== false) {
        @unlink($offlineFlag);
        $d = @json_decode($json, true);
        if (isset($d['total_power'])) return (float)$d['total_power'];
        if (isset($d['emeters'])) {
            $p = 0; foreach($d['emeters'] as $em) $p += ($em['power'] ?? 0); return $p;
        }
        if (isset($d['meters'])) {
            $p = 0; foreach($d['meters'] as $m) $p += ($m['power'] ?? 0); return $p;
        }
    }

    $json2 = @file_get_contents("http://$ip/rpc/Shelly.GetStatus", false, $ctx);
    if ($json2 !== false) {
        @unlink($offlineFlag);
        $d2 = @json_decode($json2, true);
        $p = 0; $found = false;
        if (is_array($d2)) {
            if (isset($d2['result'])) $d2 = $d2['result'];
            foreach ($d2 as $k => $v) {
                if ((strpos((string)$k, 'emeter:') === 0 || strpos((string)$k, 'em:') === 0) && isset($v['total_act_power'])) {
                    $p += (float)$v['total_act_power'];
                    $found = true;
                }
                elseif (strpos((string)$k, 'switch:') === 0 && isset($v['apower'])) {
                    $p += (float)$v['apower'];
                    $found = true;
                }
            }
        }
        if ($found) return $p;
    }

    // Wenn beide fehlschlagen, IP für 15 Sekunden auf die Ignore-Liste setzen
    @file_put_contents($offlineFlag, "offline");
    return false;
}

function liveStiebelInterpolatedWpDayKwh($historyLines, $data) {
    if ((string)($data['wp_type'] ?? '') !== '4') return null;
    if (!isset($data['e_wp']) || !is_numeric($data['e_wp'])) return null;
    $rawCounter = max(0.0, (float)$data['e_wp']);
    $externalPowerSource = strtolower((string)($data['stiebel_external_power_source'] ?? $data['stiebel_power_source'] ?? ''));
    $hasExternalPowerMeter = (
        isset($data['stiebel_external_power_w'])
        || strpos($externalPowerSource, 'shelly') !== false
        || strpos($externalPowerSource, 'external') !== false
    );
    $powerFloorW = $hasExternalPowerMeter ? 20.0 : 150.0;
    $today = date('Y-m-d');
    $samples = [];

    foreach ($historyLines as $ln) {
        $d = @json_decode($ln, true);
        if (!$d || !isset($d['ts']) || strpos((string)$d['ts'], $today) !== 0) continue;
        $ts = strtotime((string)$d['ts']);
        if ($ts === false) continue;
        $wp = isset($d['wp']) && is_numeric($d['wp']) ? max(0.0, (float)$d['wp']) : 0.0;
        $counter = (isset($d['e_wp']) && is_numeric($d['e_wp'])) ? max(0.0, (float)$d['e_wp']) : null;
        $samples[] = ['ts' => $ts, 'wp' => $wp, 'e' => $counter];
    }

    $samples[] = [
        'ts' => time(),
        'wp' => isset($data['wp']) && is_numeric($data['wp']) ? max(0.0, (float)$data['wp']) : 0.0,
        'e' => $rawCounter,
    ];
    usort($samples, fn($a, $b) => $a['ts'] <=> $b['ts']);

    if ($hasExternalPowerMeter) {
        $integrated = 0.0;
        $prev = null;
        foreach ($samples as $sample) {
            if ($prev !== null) {
                $dt = (int)$sample['ts'] - (int)$prev['ts'];
                if ($dt > 0 && $dt < 1800) {
                    $avgWp = max(0.0, ((float)$sample['wp'] + (float)$prev['wp']) / 2.0);
                    if ($avgWp > $powerFloorW) {
                        $integrated += ($avgWp * ($dt / 3600.0)) / 1000.0;
                    }
                }
            }
            $prev = $sample;
        }
        return round(max($rawCounter, $integrated), 3);
    }

    $anchorTs = null;
    $anchorCounter = $rawCounter;
    $lastCounter = null;
    foreach ($samples as $sample) {
        if ($sample['e'] === null) continue;
        if ($lastCounter === null || abs((float)$sample['e'] - (float)$lastCounter) > 0.01) {
            $anchorTs = (int)$sample['ts'];
            $anchorCounter = (float)$sample['e'];
        }
        $lastCounter = (float)$sample['e'];
    }
    if ($anchorTs === null) return round($rawCounter, 3);

    $integrated = 0.0;
    $prev = null;
    foreach ($samples as $sample) {
        if ((int)$sample['ts'] < $anchorTs) continue;
        if ($prev !== null) {
            $dt = (int)$sample['ts'] - (int)$prev['ts'];
            if ($dt > 0 && $dt < 1800) {
                $avgWp = max(0.0, ((float)$sample['wp'] + (float)$prev['wp']) / 2.0);
                if ($avgWp > $powerFloorW) {
                    $integrated += ($avgWp * ($dt / 3600.0)) / 1000.0;
                }
            }
        }
        $prev = $sample;
    }

    return round(max($rawCounter, $anchorCounter + $integrated), 3);
}

function openwbTotalRangeContract($openwbData, $nowTs = null) {
    if (!is_array($openwbData)) return null;

    $now = is_numeric($nowTs) ? (float)$nowTs : (float)time();
    $source = strtolower(trim((string)($openwbData['car_range_source'] ?? '')));
    $range = $openwbData['car_range'] ?? null;
    if (!in_array($source, ['http_total', 'mqtt_total'], true)
        || ($openwbData['car_range_valid'] ?? false) !== true
        || !is_numeric($range)
        || (float)$range <= 0.0) {
        return null;
    }

    $observedTs = $openwbData['car_range_observed_ts'] ?? null;
    if (!is_numeric($observedTs)) return null;
    $observedTs = (float)$observedTs;
    $observedAge = $now - $observedTs;
    if ($observedTs <= 0.0 || $observedAge < -5.0 || $observedAge > 120.0) {
        return null;
    }

    $sourceTsExplicit = ($openwbData['car_range_source_ts_explicit'] ?? false) === true;
    $sourceTs = $openwbData['car_range_source_ts'] ?? null;
    if ($sourceTsExplicit) {
        if (!is_numeric($sourceTs)) return null;
        $sourceTs = (float)$sourceTs;
        $sourceAge = $now - $sourceTs;
        if ($sourceTs <= 0.0 || $sourceAge < -5.0 || $sourceAge > 8 * 3600.0) {
            return null;
        }
    } else {
        // Ältere openWB-Versionen besitzen keine eigene Quellenzeit. Sie
        // bleiben ausschließlich über die frische lokale Beobachtung gültig.
        $sourceTs = $observedTs;
    }

    $vehicleKey = trim((string)($openwbData['car_range_vehicle_key'] ?? ''));
    if ($vehicleKey !== '') {
        if (($openwbData['stable_vehicle_identity_current'] ?? false) !== true) {
            return null;
        }
        $currentVehicleKeys = [];
        foreach (['vehicle_id', 'rfid_tag', 'car_id'] as $key) {
            $value = trim((string)($openwbData[$key] ?? ''));
            if ($value !== '') $currentVehicleKeys[] = $value;
        }
        if (!$currentVehicleKeys || !in_array($vehicleKey, $currentVehicleKeys, true)) {
            return null;
        }
    }

    return [
        'range_km' => (float)$range,
        'car_range_source' => $source,
        'car_range_valid' => true,
        'car_range_observed_ts' => (int)$observedTs,
        'car_range_source_ts' => (int)$sourceTs,
        'car_range_source_ts_explicit' => $sourceTsExplicit,
        'car_range_vehicle_key' => $vehicleKey,
    ];
}

function pickOpenwbTotalRangeKm($openwbData) {
    $contract = openwbTotalRangeContract($openwbData);
    return is_array($contract) ? (float)$contract['range_km'] : 0.0;
}

function liveWallboxApparentKva($wallboxData) {
    if (!is_array($wallboxData)) return 0.0;
    $va = 0.0;
    if (isset($wallboxData['apparent_power_va']) && is_numeric($wallboxData['apparent_power_va'])) {
        $va = max(0.0, (float)$wallboxData['apparent_power_va']);
    }
    if ($va <= 0.0 && isset($wallboxData['apparent_power_kva']) && is_numeric($wallboxData['apparent_power_kva'])) {
        return round(max(0.0, (float)$wallboxData['apparent_power_kva']), 2);
    }
    if ($va <= 0.0) {
        foreach (['phase_apparent_l1_va', 'phase_apparent_l2_va', 'phase_apparent_l3_va'] as $key) {
            if (isset($wallboxData[$key]) && is_numeric($wallboxData[$key])) {
                $va += max(0.0, (float)$wallboxData[$key]);
            }
        }
    }
    return $va > 50.0 ? round($va / 1000.0, 2) : 0.0;
}

function liveWallboxPowerFactor($wallboxData, $powerW = 0.0) {
    if (!is_array($wallboxData)) return 0.0;
    if (isset($wallboxData['power_factor']) && is_numeric($wallboxData['power_factor']) && (float)$wallboxData['power_factor'] > 0) {
        return round(max(0.0, min(1.0, (float)$wallboxData['power_factor'])), 2);
    }
    $kva = liveWallboxApparentKva($wallboxData);
    return $kva > 0.05 ? round(max(0.0, abs((float)$powerW) / ($kva * 1000.0)), 2) : 0.0;
}

function liveWallboxFineAmpFields($wallboxData, $fallbackAmp = 0.0) {
    if (!is_array($wallboxData)) $wallboxData = [];
    $fallback = is_numeric($fallbackAmp) ? max(0.0, (float)$fallbackAmp) : 0.0;
    $offered = $fallback;
    foreach (['offered_current_raw', 'last_command_amp'] as $key) {
        if (isset($wallboxData[$key]) && is_numeric($wallboxData[$key])) {
            $candidate = max(0.0, (float)$wallboxData[$key]);
            if ($candidate > 0.0 || $offered <= 0.0) {
                $offered = $candidate;
                break;
            }
        }
    }

    $step = 1.0;
    if (isset($wallboxData['current_step_amp']) && is_numeric($wallboxData['current_step_amp'])) {
        $stepCandidate = (float)$wallboxData['current_step_amp'];
        if ($stepCandidate > 0.0) $step = $stepCandidate;
    }

    $fractionalRaw = $wallboxData['fractional_current_supported'] ?? false;
    $fractional = $fractionalRaw === true
        || $fractionalRaw === 1
        || $fractionalRaw === '1'
        || strtolower(trim((string)$fractionalRaw)) === 'true'
        || $step <= 0.11
        || ($offered > 0.0 && abs($offered - round($offered)) > 0.001);

    return [
        'offered_current_raw' => round($offered, 1),
        'current_step_amp' => round(max(0.1, $step), 1),
        'fractional_current_supported' => $fractional,
    ];
}

function liveApplyWallboxFineAmpFields(&$data, $prefix, $wallboxData, $fallbackAmp = 0.0) {
    $fineAmp = liveWallboxFineAmpFields($wallboxData, $fallbackAmp);
    $data[$prefix . '_offered_current_raw'] = $fineAmp['offered_current_raw'];
    $data[$prefix . '_current_step_amp'] = $fineAmp['current_step_amp'];
    $data[$prefix . '_fractional_current_supported'] = $fineAmp['fractional_current_supported'];
}

function liveResolveE3dcMultiHomeRelation(&$data, $slot = 'wb') {
    $slot = ($slot === 'wb2') ? 'wb2' : 'wb';
    $powerKey = $slot;
    $flagKey = ($slot === 'wb2') ? 'is_external_wb2' : 'is_external_wb';
    $relationKey = ($slot === 'wb2') ? 'wb2_home_relation' : 'wb_home_relation';
    $correctionKey = ($slot === 'wb2') ? 'wb2_home_correction_source' : 'wb_home_correction_source';
    $sourceKey = ($slot === 'wb2') ? 'wb2_source' : 'wb_source';

    $wb = isset($data[$powerKey]) && is_numeric($data[$powerKey]) ? max(0.0, (float)$data[$powerKey]) : 0.0;
    if ($wb <= 50.0) return;
    $pv = isset($data['pv']) && is_numeric($data['pv']) ? (float)$data['pv'] : 0.0;
    $grid = isset($data['grid']) && is_numeric($data['grid']) ? (float)$data['grid'] : 0.0;
    $bat = isset($data['bat']) && is_numeric($data['bat']) ? (float)$data['bat'] : 0.0;
    $rawHome = isset($data['home_raw']) && is_numeric($data['home_raw']) ? max(0.0, (float)$data['home_raw']) : null;
    if ($rawHome === null) return;

    // Same sign convention as the live chart: positive battery means charging.
    $totalLoad = $pv + $grid - $bat;
    if ($totalLoad < -500.0 || $totalLoad > 60000.0) return;

    $tolerance = max(250.0, min(1200.0, $wb * 0.18));
    $netGap = $totalLoad - $rawHome;
    $netHomeMatches = abs($netGap - $wb) <= $tolerance;
    $grossHomeMatches = abs($totalLoad - $rawHome) <= $tolerance;

    $data[$sourceKey] = $data[$sourceKey] ?? 'e3dc_multi_native';
    if ($netHomeMatches) {
        // E3DC Home_Power is already house-only; do not subtract the WB again.
        $data[$flagKey] = false;
        $data[$relationKey] = 'home_excludes_wb';
        $data[$correctionKey] = 'e3dc_multi_home_net';
        return;
    }

    if ($grossHomeMatches && ($rawHome + $tolerance) >= $wb) {
        // E3DC Home_Power is gross total load; subtract WB in the clean home bucket.
        $data[$flagKey] = true;
        $data[$relationKey] = 'home_includes_wb';
        $data[$correctionKey] = 'e3dc_multi_home_gross';
        return;
    }

    if ($grossHomeMatches && ($rawHome + $tolerance) < $wb) {
        // Native WB power is not present in the system balance anymore. Treat it
        // as a stale handover value so it cannot pollute live history or longterm.
        $data[($slot === 'wb2') ? 'wb2_suppressed_power_w' : 'wb_suppressed_power_w'] = $wb;
        $data[$powerKey] = 0.0;
        $data[$flagKey] = false;
        $data[$relationKey] = 'stale_balance_reject';
        $data[$correctionKey] = 'e3dc_multi_stale_balance_reject';
        return;
    }

    $data[$relationKey] = 'uncertain';
    $data[$correctionKey] = 'e3dc_multi_uncertain';
}

function liveApplyE3dcMultiHomeRelationFromConfig(&$data, $confData) {
    $config = (isset($confData['config']) && is_array($confData['config'])) ? $confData['config'] : [];
    foreach (['wb' => 'wb_native_type', 'wb2' => 'wb_native_type2'] as $slot => $configKey) {
        $type = normalizeWallboxTypeConfig($config[$configKey] ?? '');
        if (!in_array($type, ['e3dc_multi', 'e3dc_multi_connect'], true)) continue;
        $relationKey = ($slot === 'wb2') ? 'wb2_home_relation' : 'wb_home_relation';
        $sourceKey = ($slot === 'wb2') ? 'wb2_source' : 'wb_source';
        $nativeTypeKey = ($slot === 'wb2') ? 'wb2_native_type' : 'wb_native_type';
        $power = isset($data[$slot]) && is_numeric($data[$slot]) ? (float)$data[$slot] : 0.0;
        $data[$nativeTypeKey] = $data[$nativeTypeKey] ?? $type;
        if ($power <= 50.0 || !empty($data[$relationKey])) continue;
        $data[$sourceKey] = $data[$sourceKey] ?? 'e3dc_multi_native';
        liveResolveE3dcMultiHomeRelation($data, $slot);
    }
}

function stabilizeCleanHomePower(&$data) {
    $pv = isset($data['pv']) && is_numeric($data['pv']) ? (float)$data['pv'] : 0.0;
    $grid = isset($data['grid']) && is_numeric($data['grid']) ? (float)$data['grid'] : 0.0;
    $bat = isset($data['bat']) && is_numeric($data['bat']) ? (float)$data['bat'] : 0.0;
    $wb = isset($data['wb']) && is_numeric($data['wb']) ? max(0.0, (float)$data['wb']) : 0.0;
    $wb2 = isset($data['wb2']) && is_numeric($data['wb2']) ? max(0.0, (float)$data['wb2']) : 0.0;
    $wp = isset($data['wp']) && is_numeric($data['wp']) ? max(0.0, (float)$data['wp']) : 0.0;
    $hs = isset($data['hs_power']) && is_numeric($data['hs_power']) ? max(0.0, (float)$data['hs_power']) : 0.0;
    $climate = isset($data['climate_power_w']) && is_numeric($data['climate_power_w']) ? max(0.0, (float)$data['climate_power_w']) : 0.0;
    $currentHome = isset($data['home']) && is_numeric($data['home'])
        ? max(0.0, (float)$data['home'])
        : max(0.0, (float)($data['home_raw'] ?? 0));
    $externalConsumerW = 0.0;
    if (!empty($data['is_external_wb'])) $externalConsumerW += $wb;
    if (!empty($data['is_external_wb2'])) $externalConsumerW += $wb2;
    $externalConsumerW += $wp + $hs + $climate;
    $hasExternalConsumer = $externalConsumerW > 500.0;

    // Physikalische Bilanz mit finalen WB/WP-Messwerten:
    // PV + Netz(Bezug positiv) - Batterie(Laden positiv) = alle Verbraucher.
    $totalLoad = $pv + $grid - $bat;
    $balanceHome = $totalLoad - $wb - $wb2 - $wp - $hs - $climate;
    if ($totalLoad > -500.0 && $totalLoad < 60000.0 && $balanceHome > -500.0 && $balanceHome < 30000.0) {
        $balanceHome = max(0.0, round($balanceHome));
        $useBalance = false;
        if ($hasExternalConsumer) {
            // Bei Fremd-Wallboxen ist home_raw minus echte WB-Leistung stabiler
            // als PV/Grid/Batterie-Bilanz, weil diese Messungen asynchron kommen.
            $useBalance = ($currentHome < 50.0 && $balanceHome > 150.0);
        } else {
            // E3DC Home_Power ist die fuehrende Hauslast. Die PV/Grid/Batterie-
            // Bilanz kann bei Akku-/Netz-Vorzeichen und asynchronen Messungen
            // mehrere kW daneben liegen; sie darf nur echte Null-/Ausfallwerte
            // ersetzen, aber keine plausible Hauslast hochziehen.
            $useBalance = ($currentHome < 50.0 && $balanceHome > 150.0);
        }
        if ($useBalance) {
            $data['home'] = $balanceHome;
            $data['home_source'] = 'energy_balance';
        }
    }

    $stateFile = '/var/www/html/ramdisk/home_clean_filter.json';
    $previousState = [];
    if (is_readable($stateFile)) {
        $rawState = @file_get_contents($stateFile);
        $decodedState = is_string($rawState) ? json_decode($rawState, true) : null;
        if (is_array($decodedState)) {
            $previousState = $decodedState;
        }
    }

    $home = max(0.0, (float)($data['home'] ?? $currentHome));
    $nowFloat = microtime(true);
    $homeCandidate = max(0.0, min(30000.0, $home));
    $displayHomeCandidate = round($homeCandidate);
    $rawHomeForHold = isset($data['home_raw']) && is_numeric($data['home_raw'])
        ? max(0.0, (float)$data['home_raw'])
        : 0.0;
    $previousHome = isset($previousState['home']) && is_numeric($previousState['home'])
        ? max(0.0, min(30000.0, (float)$previousState['home']))
        : null;
    $previousSeen = isset($previousState['last_seen_float']) && is_numeric($previousState['last_seen_float'])
        ? (float)$previousState['last_seen_float']
        : (isset($previousState['ts_float']) && is_numeric($previousState['ts_float'])
            ? (float)$previousState['ts_float']
            : (isset($previousState['ts']) && is_numeric($previousState['ts']) ? (float)$previousState['ts'] : null));
    $previousRealSeen = isset($previousState['last_plausible_float']) && is_numeric($previousState['last_plausible_float'])
        ? (float)$previousState['last_plausible_float']
        : $previousSeen;
    $previousAgeS = ($previousRealSeen !== null) ? max(0.0, $nowFloat - $previousRealSeen) : null;
    $externalDeltaW = abs($rawHomeForHold - $externalConsumerW);
    $zeroGlitchCandidate = (
        $displayHomeCandidate <= 50.0
        && (
            ($rawHomeForHold > 500.0 && $externalConsumerW > 500.0 && $externalDeltaW <= max(350.0, $externalConsumerW * 0.25))
            || $rawHomeForHold <= 50.0
            || $displayHomeCandidate <= 0
        )
    );
    $heldZeroGlitch = false;
    if (
        $zeroGlitchCandidate
        && $previousHome !== null
        && $previousHome > 80.0
        && $previousHome < 30000.0
        && $previousAgeS !== null
        && $previousAgeS <= 180.0
    ) {
        $data['home'] = round($previousHome);
        if ($rawHomeForHold <= 50.0) {
            $data['home_source'] = 'held_rscp_zero_glitch';
        } else {
            $data['home_source'] = 'held_external_consumer_zero_glitch';
        }
        $data['home_held_zero_glitch'] = true;
        $heldZeroGlitch = true;
    } else {
        $data['home'] = $displayHomeCandidate;
    }
    $lastPlausibleFloat = ($data['home'] > 80)
        ? ($heldZeroGlitch ? $previousRealSeen : $nowFloat)
        : $previousRealSeen;

    @file_put_contents($stateFile, json_encode([
        'home' => $data['home'],
        'ts' => time(),
        'ts_float' => $nowFloat,
        'last_seen_float' => $nowFloat,
        'last_plausible_float' => $lastPlausibleFloat,
        'home_candidate' => round($homeCandidate),
        'home_previous' => $previousHome !== null ? round($previousHome) : null,
        'held_zero_glitch' => $heldZeroGlitch,
        'home_hold_reason' => $heldZeroGlitch ? 'external_consumer_subtraction_zero' : null,
        'home_median' => null,
        'samples' => [],
    ]), LOCK_EX);
}

function openwbDailyHangoverSeen($wallboxNo, $rawKwh) {
    $rawKwh = (float)$rawKwh;
    if ($rawKwh <= 0.05) return false;

    $historyFile = '/var/www/html/ramdisk/live_history.txt';
    if (!is_readable($historyFile)) return false;

    $today = date('Y-m-d');
    $energyKey = ((int)$wallboxNo === 2) ? 'e_wb2' : 'e_wb';
    $powerKey = ((int)$wallboxNo === 2) ? 'wb2' : 'wb';
    $samples = 0;
    $sameIdle = 0;
    $activePower = 0;

    $lines = @file($historyFile, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    if (!is_array($lines)) return false;

    foreach ($lines as $ln) {
        $d = @json_decode($ln, true);
        if (!is_array($d) || empty($d['ts']) || strpos((string)$d['ts'], $today) !== 0) continue;
        if (!isset($d[$energyKey]) || !is_numeric($d[$energyKey])) continue;

        $samples++;
        $pwr = isset($d[$powerKey]) && is_numeric($d[$powerKey]) ? abs((float)$d[$powerKey]) : 0.0;
        if ($pwr > 100.0) $activePower++;
        if (abs((float)$d[$energyKey] - $rawKwh) < 0.03 && $pwr < 100.0) $sameIdle++;
        if ($samples >= 20) break;
    }

    return ($samples >= 2 && $sameIdle >= 2 && $activePower == 0);
}

function normalizeOpenwbDailyKwh($wallboxNo, $rawWh, $powerW = 0.0, $charging = false, $sessionKwh = 0.0) {
    $rawWh = max(0.0, (float)$rawWh);
    $rawKwh = $rawWh / 1000.0;
    $powerW = abs((float)$powerW);
    $sessionWh = max(0.0, (float)$sessionKwh * 1000.0);
    $active = $charging || $powerW > 100.0 || $sessionWh > 50.0;
    $today = date('Y-m-d');
    $key = ((int)$wallboxNo === 2) ? 'wb2' : 'wb1';
    $stateFile = '/var/www/html/ramdisk/openwb_daily_baseline.json';
    $state = [];

    if (is_readable($stateFile)) {
        $decoded = @json_decode(@file_get_contents($stateFile), true);
        if (is_array($decoded)) $state = $decoded;
    }

    $entry = isset($state[$key]) && is_array($state[$key]) ? $state[$key] : null;
    $newDay = !$entry || (($entry['date'] ?? '') !== $today);
    if ($newDay) {
        // At a real day change the current openWB counter is the baseline.
        // If WebUI is first started mid-session, use the session anchor.
        $baselineWh = $active ? max(0.0, $rawWh - $sessionWh) : $rawWh;
    } else {
        $baselineWh = max(0.0, (float)($entry['baseline_wh'] ?? 0.0));
    }

    // Some openWB/openWB-Pro counters behave like "since plug-in" counters.
    // If the UI was updated after midnight and history already shows the same
    // idle value from 00:00 onward, treat it as yesterday's carry-over.
    if (!$entry && !$active && openwbDailyHangoverSeen($wallboxNo, $rawKwh)) {
        $baselineWh = $rawWh;
    }

    // True daily counters may reset to zero. Drop the baseline then.
    if ($rawWh + 500.0 < $baselineWh) {
        $baselineWh = 0.0;
    }

    $dailyWh = max(0.0, $rawWh - $baselineWh);
    $state[$key] = [
        'date' => $today,
        'baseline_wh' => round($baselineWh, 1),
        'last_raw_wh' => round($rawWh, 1),
        'last_daily_wh' => round($dailyWh, 1),
        'ts' => time(),
    ];
    @file_put_contents($stateFile, json_encode($state), LOCK_EX);
    @chmod($stateFile, 0664);

    return [
        'kwh' => round($dailyWh / 1000.0, 3),
        'raw_kwh' => round($rawKwh, 3),
        'baseline_kwh' => round($baselineWh / 1000.0, 3),
        'source' => $baselineWh > 0.0 ? 'openwb_daily_imported_delta' : 'openwb_daily_imported',
    ];
}

function normalizeVehicleMergeKey($name) {
    $name = trim((string)$name);
    $name = preg_replace('/\s+/u', ' ', $name);
    $name = function_exists('mb_strtolower') ? mb_strtolower($name, 'UTF-8') : strtolower($name);
    return $name ?: '';
}

function vehicleRecordHasBoundDedupeIdentity($vehicle) {
    if (!is_array($vehicle)) return false;
    if (trim((string)($vehicle['profile_id'] ?? '')) !== '') return true;

    $id = trim((string)($vehicle['id'] ?? ''));
    if ($id === '' || in_array($id, ['openwb_native', 'openwb_pro_wb2'], true)) {
        return false;
    }
    foreach (['vehicle_', 'openwb_observed_', 'manual_wb'] as $prefix) {
        if (strpos($id, $prefix) === 0) return false;
    }
    return true;
}

function vehicleNameFallbackMergeIndex($nameKey, $vehicleIndexByName, $identityBoundByIndex, $currentIdentityBound) {
    if ($nameKey === ''
        || !array_key_exists($nameKey, $vehicleIndexByName)
        || $vehicleIndexByName[$nameKey] === null) {
        return null;
    }

    $candidateIndex = (int)$vehicleIndexByName[$nameKey];
    $candidateIdentityBound = ($identityBoundByIndex[$candidateIndex] ?? false) === true;
    // Ein Anzeigename ist keine Fahrzeugidentität. Der Legacy-Fallback ist nur
    // zulässig, wenn genau eine Seite bereits an ein stabiles Profil gebunden
    // ist. Zwei ungebundene Beobachtungen dürfen ebenso wenig geraten werden wie
    // zwei verschiedene stabile Profile.
    if ($currentIdentityBound === $candidateIdentityBound) return null;
    return $candidateIndex;
}

function legacySessionSingleVehicleIndex($vehicles) {
    if (!is_array($vehicles)) return -1;
    $groups = [];
    foreach ($vehicles as $index => $vehicle) {
        if (!is_array($vehicle)) continue;
        if (vehicleRecordHasBoundDedupeIdentity($vehicle)) {
            $stableId = trim((string)($vehicle['profile_id'] ?? ($vehicle['id'] ?? '')));
            if ($stableId === '') continue;
            $groupKey = 'stable:' . compactVehicleIdentifier($stableId);
        } else {
            // Jede ungebundene Beobachtung ist eine eigene mögliche Identität.
            $groupKey = 'unbound:' . (string)$index;
        }
        if (!isset($groups[$groupKey])) $groups[$groupKey] = (int)$index;
    }
    return count($groups) === 1 ? (int)reset($groups) : -1;
}

function registerVehicleNameMergeIndex($vehicleIndexByName, $nameKey, $mergeIndex) {
    if ($nameKey === '') return $vehicleIndexByName;
    $mergeIndex = (int)$mergeIndex;
    if (!array_key_exists($nameKey, $vehicleIndexByName)) {
        $vehicleIndexByName[$nameKey] = $mergeIndex;
    } elseif ($vehicleIndexByName[$nameKey] !== null
        && (int)$vehicleIndexByName[$nameKey] !== $mergeIndex) {
        // Ab dem zweiten getrennten Datensatz ist der Name mehrdeutig und darf
        // auch für spätere ungebundene Datensätze nicht mehr geraten werden.
        $vehicleIndexByName[$nameKey] = null;
    }
    return $vehicleIndexByName;
}

function compactVehicleIdentifier($value) {
    return preg_replace('/[^a-z0-9]/', '', strtolower(trim((string)$value)));
}

function firstCompactVehicleIdentifier($values) {
    foreach ((array)$values as $value) {
        $compact = compactVehicleIdentifier($value);
        if ($compact !== '') return $compact;
    }
    return '';
}

function e3dcHeatOwnerInfo($owner) {
    $owner = strtolower(trim((string)$owner));
    switch ($owner) {
        case 'predump_heatpump':
            return [
                'label' => 'Pre-Dump',
                'reason' => 'Storage Manager gibt die Wärmepumpe für Pre-Dump frei.',
                'kind' => 'predump',
            ];
        case 'price_plan_heatpump':
            return [
                'label' => 'Preisfenster',
                'reason' => 'Explizites Preis-/Tariffenster gibt die Wärmepumpe frei.',
                'kind' => 'price',
            ];
        case 'market_plan_heatpump':
            return [
                'label' => 'Marktfenster',
                'reason' => 'Storage-Marktvertrag gibt die Wärmepumpe prognose- und margenbasiert frei.',
                'kind' => 'market',
            ];
        case 'legacy_price_heatpump':
            return [
                'label' => 'Preis-Boost',
                'reason' => 'Legacy-Preislogik des Energy Managers ist aktiv.',
                'kind' => 'price_legacy',
            ];
        case 'legacy_pv_pause':
        case 'source_recovery_heatpump':
        case 'quell_erholung':
            return [
                'label' => 'Quell-Erholung',
                'reason' => 'Pausenmodus für Wärmequelle und Laufzeitlogik ist aktiv.',
                'kind' => 'source_recovery',
            ];
        case 'manual_heatpump':
        case 'manual_ww_heatpump':
            return [
                'label' => 'Manuell',
                'reason' => 'Manuelle Wärmefreigabe läuft.',
                'kind' => 'manual',
            ];
        case 'storage_budget_heatpump':
            return [
                'label' => 'Wärmebudget',
                'reason' => 'Storage Manager bietet Budget für Wärmepumpe oder Heizstab an.',
                'kind' => 'budget',
            ];
        default:
            return [
                'label' => 'Beobachtet',
                'reason' => 'Kein aktiver Wärmeauftrag; Energy Manager beobachtet nur.',
                'kind' => 'observe',
            ];
    }
}

function vehicleSocTimestamp($value, $now = null) {
    if ($value === null || $value === '') return null;
    if (is_numeric($value)) {
        $ts = (float)$value;
        if ($ts > 100000000000.0) $ts /= 1000.0;
    } elseif (is_string($value)) {
        $parsed = strtotime(trim($value));
        if ($parsed === false) return null;
        $ts = (float)$parsed;
    } else {
        return null;
    }
    $now = is_numeric($now) ? (float)$now : (float)time();
    if (!is_finite($ts) || $ts <= 0.0 || $ts > ($now + 300.0)) return null;
    return (int)round($ts);
}

function vehicleSocRecordTimestamp($vehicle, $now = null) {
    if (!is_array($vehicle)) return null;
    // Der Quellzeitpunkt gehört zum Ist-SoC. Ein zyklisch neuer Datei- oder
    // Mergezeitpunkt darf einen alten Rohanker niemals künstlich verjüngen.
    if (array_key_exists('soc_source_ts', $vehicle)) {
        $sourceTs = vehicleSocTimestamp($vehicle['soc_source_ts'], $now);
        if ($sourceTs !== null) return $sourceTs;
        if (array_key_exists('raw_soc_ts', $vehicle)) {
            return vehicleSocTimestamp($vehicle['raw_soc_ts'], $now);
        }
        return null;
    }
    if (array_key_exists('raw_soc_ts', $vehicle)) {
        return vehicleSocTimestamp($vehicle['raw_soc_ts'], $now);
    }
    $source = strtolower(trim((string)($vehicle['soc_source'] ?? ($vehicle['source'] ?? ''))));
    $manualSource = in_array($source, ['manual_start_soc', 'manual_soc', 'manual', 'openwb_profile_link'], true);
    // Nur bekannte manuelle Altverträge dürfen ihren damaligen Aktionszeitpunkt
    // aus `ts` übernehmen. `last_updated_at` ist auch dort lediglich Datei- oder
    // Mergezeit und darf weder Cloud- noch Maschinenwerte verjüngen.
    return $manualSource && array_key_exists('ts', $vehicle)
        ? vehicleSocTimestamp($vehicle['ts'], $now)
        : null;
}

function vehicleSocCloudFreshnessSeconds($config = []) {
    return e3dcVehicleSocCloudFreshnessSeconds($config);
}

function vehicleSocResolvedCloudFreshnessSeconds(
    $config = [],
    $v4Path = '/var/www/html/data/e3dc_v4.json',
    $txtPath = null
) {
    $config = is_array($config) ? $config : [];
    $topLevelBound = array_key_exists('bluelink_interval', $config);
    $interval = $topLevelBound ? $config['bluelink_interval'] : '15';

    if (!$topLevelBound) {
        $v4Raw = e3dcReadRegularFileBound((string)$v4Path, 1048576);
        $v4 = is_string($v4Raw) ? @json_decode($v4Raw, true) : null;
        $nested = is_array($v4) && isset($v4['config']) && is_array($v4['config'])
            ? $v4['config']
            : [];
        if (array_key_exists('bluelink_interval', $nested)) {
            $value = $nested['bluelink_interval'];
            if ((is_scalar($value) || $value === null) && trim((string)$value) !== '') {
                $interval = (string)$value;
            }
        }
    }

    // Der Client behält für Bestandsinstallationen denselben TXT-Fallback:
    // Ein expliziter Top-Level-V4-Wert bindet, sonst darf der Default 15 aus
    // einer verschachtelten/fehlenden V4-Konfiguration überschrieben werden.
    if (!$topLevelBound && trim((string)$interval) === '15'
        && is_string($txtPath) && $txtPath !== '') {
        $txtRaw = e3dcReadRegularFileBound($txtPath, 1048576);
        if (is_string($txtRaw)) {
            foreach (preg_split('/\R/', $txtRaw) ?: [] as $line) {
                $trimmed = trim((string)$line);
                if ($trimmed === '' || str_starts_with($trimmed, '#') || strpos($line, '=') === false) continue;
                [$key, $value] = array_map('trim', explode('=', $line, 2));
                if (strtolower($key) === 'bluelink_interval') {
                    $interval = $value;
                    break;
                }
            }
        }
    }

    return vehicleSocCloudFreshnessSeconds(['bluelink_interval' => $interval]);
}

function vehicleBluelinkRefreshProjection($payload, $now = null) {
    if (!is_array($payload)) return null;
    $refresh = $payload['refresh'] ?? null;
    if (!is_array($refresh)
        || ($refresh['schema'] ?? null) !== 'bluelink_refresh_status_v1') {
        return null;
    }
    $now = is_numeric($now) ? (int)$now : time();
    $status = (string)($refresh['status'] ?? '');
    $mode = (string)($refresh['mode'] ?? '');
    if (!in_array($status, [
        'failed', 'success_source_unknown',
        'success_source_advanced', 'success_source_unchanged',
        'success_source_partial',
    ], true) || !in_array($mode, ['cached', 'force'], true)) {
        return null;
    }
    $attemptTs = vehicleSocTimestamp($refresh['attempt_ts'] ?? null, $now);
    $completedTs = vehicleSocTimestamp($refresh['completed_ts'] ?? null, $now);
    $sourceTs = vehicleSocTimestamp($refresh['source_ts'] ?? null, $now);
    $responseContractPresent = array_key_exists('response_source_complete', $refresh);
    $responseSourceTs = vehicleSocTimestamp(
        $responseContractPresent
            ? ($refresh['response_source_ts'] ?? null)
            : ($refresh['source_ts'] ?? null),
        $now
    );
    $responseSourceComplete = $responseContractPresent
        ? (($refresh['response_source_complete'] ?? null) === true)
        : in_array($status, ['success_source_advanced', 'success_source_unchanged'], true);
    $responseVehicleCount = max(0, (int)($refresh['response_vehicle_count'] ?? 0));
    $responseMissingSourceCount = max(
        0,
        min(
            $responseVehicleCount,
            (int)($refresh['response_missing_source_count'] ?? 0)
        )
    );
    $lastErrorRaw = is_array($refresh['last_error'] ?? null)
        ? $refresh['last_error']
        : null;
    $lastError = null;
    if ($lastErrorRaw !== null) {
        $lastErrorTs = vehicleSocTimestamp($lastErrorRaw['ts'] ?? null, $now);
        $lastErrorMode = (string)($lastErrorRaw['mode'] ?? '');
        $lastErrorCode = (string)($lastErrorRaw['code'] ?? '');
        if ($lastErrorTs !== null
            && in_array($lastErrorMode, ['cached', 'force'], true)
            && in_array($lastErrorCode, [
                'timeout', 'rate_limited', 'authentication_failed', 'vehicle_data_missing', 'api_error',
            ], true)) {
            $lastError = [
                'ts' => $lastErrorTs,
                'age_s' => max(0, $now - $lastErrorTs),
                'mode' => $lastErrorMode,
                'code' => $lastErrorCode,
                'message' => trim((string)($lastErrorRaw['message'] ?? '')),
            ];
        }
    }
    $lastErrorActive = $lastError !== null
        && (!$responseSourceComplete
            || $responseSourceTs === null
            || $responseSourceTs <= (int)$lastError['ts']);

    return [
        'schema' => 'bluelink_refresh_status_v1',
        'status' => $status,
        'mode' => $mode,
        'success' => ($refresh['success'] ?? null) === true,
        'attempt_ts' => $attemptTs,
        'attempt_age_s' => $attemptTs !== null ? max(0, $now - $attemptTs) : null,
        'completed_ts' => $completedTs,
        'completed_age_s' => $completedTs !== null ? max(0, $now - $completedTs) : null,
        // Das Alter wird immer aus dem Fahrzeuganker neu berechnet. Die
        // producerseitige Zahl und die Datei-Mtime sind keine Wahrheit.
        'source_ts' => $sourceTs,
        'source_age_s' => $sourceTs !== null ? max(0, $now - $sourceTs) : null,
        'source_advanced' => ($refresh['source_advanced'] ?? null) === true,
        'response_source_ts' => $responseSourceTs,
        'response_source_complete' => $responseSourceComplete,
        'response_vehicle_count' => $responseVehicleCount,
        'response_missing_source_count' => $responseMissingSourceCount,
        'error_code' => in_array((string)($refresh['error_code'] ?? ''), [
            'timeout', 'rate_limited', 'authentication_failed', 'vehicle_data_missing', 'api_error',
        ], true) ? (string)$refresh['error_code'] : null,
        'message' => trim((string)($refresh['message'] ?? '')),
        'last_error' => $lastError,
        'last_error_active' => $lastErrorActive,
    ];
}

function vehicleSocSourceNotOlderThan($vehicle, $incomingSourceTs, $slackS = 120) {
    $incomingTs = vehicleSocTimestamp($incomingSourceTs);
    if ($incomingTs === null) return false;
    $existingTs = vehicleSocRecordTimestamp($vehicle);
    $slackS = is_numeric($slackS) ? max(0, (int)$slackS) : 0;
    return $existingTs === null || ($incomingTs + $slackS) >= $existingTs;
}

function vehicleSocSourcePriority($source, $isInterpolated = false, $lastUpdatedAt = null, $cloudFreshnessS = 900) {
    $source = strtolower(trim((string)$source));
    $sourceTs = vehicleSocTimestamp($lastUpdatedAt);
    $age = $sourceTs !== null ? max(0, time() - $sourceTs) : null;
    if (in_array($source, ['bluelink', 'hyundai', 'kia', 'cloud', 'vehicle_cloud'], true)) {
        $cloudFreshnessS = is_numeric($cloudFreshnessS) ? max(900, (int)$cloudFreshnessS) : 900;
        return ($age === null || $age > $cloudFreshnessS) ? 3 : 5;
    }
    if ($source === 'mqtt') {
        return ($age === null || $age > 8 * 3600) ? 1 : 4;
    }
    if (in_array($source, ['openwb_pro_raw', 'ccs_wallbox', 'ccs_wallbox_wb2', 'openwb_mqtt'], true)) return 4;
    if (in_array($source, ['openwb_pro_estimated', 'manual_soc', 'manual'], true)) return 3;
    // Eine frische, profilgebundene Interpolation aus einem bestätigten
    // Fahrzeuganker darf einen inzwischen alten Cloud-Anker fortschreiben.
    // Roh- und Eigenwerte der openWB Pro bleiben im Merge trotzdem vorrangig.
    if (strpos($source, 'wallbox_estimated_from_') === 0 && wallboxSocSourceTrusted($source)) return 3;
    return !empty($isInterpolated) ? 2 : 1;
}

function wallboxSocSourceTrusted($source) {
    return is_array(e3dcVehicleSocSourceContract($source));
}

function wallboxSocRuleConfirmed($source, $ruleConfirmed = null, $rulePresent = null) {
    $contract = e3dcVehicleSocSourceContract($source);
    if (!is_array($contract)) return false;
    $rulePresent = is_bool($rulePresent) ? $rulePresent : $ruleConfirmed !== null;
    $manualSource = !$contract['derived'] && $contract['kind'] === 'manual';
    if ($manualSource) {
        return !$rulePresent || $ruleConfirmed === true;
    }
    return $ruleConfirmed === true;
}

function wallboxVehicleSocRuleUsable($vehicle, $cloudFreshnessS = 900) {
    if (!is_array($vehicle)) return false;
    $source = $vehicle['soc_source'] ?? ($vehicle['source'] ?? '');
    if (!wallboxSocRuleConfirmed(
        $source,
        $vehicle['soc_rule_confirmed'] ?? null,
        array_key_exists('soc_rule_confirmed', $vehicle)
    )) return false;
    $sourceTs = vehicleSocRecordTimestamp($vehicle);
    $maxAgeS = e3dcVehicleSocPayloadMaxAgeSeconds(
        $vehicle,
        $source,
        $cloudFreshnessS
    );
    return $sourceTs !== null
        && $maxAgeS !== null
        && (time() - $sourceTs) <= $maxAgeS;
}

// Kompatibilitätsname: "confirmed" bezeichnet historisch ausschließlich die
// Regelzulässigkeit. Messqualität und Aktualität werden separat transportiert.
function wallboxVehicleSocConfirmed($vehicle) {
    return wallboxVehicleSocRuleUsable($vehicle);
}

function vehicleSocSourceClass($source, $isInterpolated = false) {
    $contract = e3dcVehicleSocSourceContract($source);
    if (!empty($isInterpolated) || (is_array($contract) && $contract['derived'])) return 'estimated';
    if (is_array($contract) && $contract['kind'] === 'cloud') return 'cloud';
    if (is_array($contract) && $contract['kind'] === 'manual') return 'manual';
    return 'observed';
}

function vehicleSocUsesOpenwbProAnchor($source) {
    $contract = e3dcVehicleSocSourceContract($source);
    return is_array($contract)
        && in_array($contract['base_source'], ['openwb_pro_raw', 'openwb_pro_estimated'], true);
}

function vehicleSocUsesMqttAnchor($source) {
    $contract = e3dcVehicleSocSourceContract($source);
    return is_array($contract)
        && in_array($contract['base_source'], ['mqtt', 'openwb_mqtt'], true);
}

function wallboxSocSourceTimestamp($payload, $source, $now = null) {
    $payload = is_array($payload) ? $payload : [];
    if (array_key_exists('car_soc_source_ts', $payload)) {
        $sourceTs = vehicleSocTimestamp(
            $payload['car_soc_source_ts'],
            $now
        );
        if ($sourceTs !== null) return $sourceTs;
        if (array_key_exists('car_soc_raw_ts', $payload)) {
            return vehicleSocTimestamp($payload['car_soc_raw_ts'], $now);
        }
        return null;
    }
    if (array_key_exists('car_soc_raw_ts', $payload)) {
        return vehicleSocTimestamp($payload['car_soc_raw_ts'], $now);
    }
    // Der zyklische openWB-Statuszeitpunkt ist für keine SoC-Quelle ein
    // Mess- oder Aktionszeitpunkt. Aktuelle Producer liefern source/raw explizit;
    // manuelle Legacy-Anker werden separat aus manual_soc_wb*.json gelesen.
    return null;
}

function wallboxSocTruthConfirmed($source, $ruleConfirmed, $sourceTs, $now = null, $rulePresent = null, $agePayload = null, $cloudFreshnessS = 900) {
    if (!wallboxSocRuleConfirmed($source, $ruleConfirmed, $rulePresent)) return false;
    // Jede regelwirksame Quelle braucht ihren eigenen Ereignisanker. Das gilt
    // auch für sonstige Maschinenquellen; ein frischer Status-Heartbeat allein
    // bestätigt weder Messwert noch manuelle Nutzeraktion.
    $now = is_numeric($now) ? (int)$now : time();
    $anchorTs = vehicleSocTimestamp($sourceTs, $now);
    if ($anchorTs === null) return false;
    $source = strtolower(trim((string)$source));
    $maxAgeS = e3dcVehicleSocPayloadMaxAgeSeconds(
        $agePayload,
        $source,
        $cloudFreshnessS
    );
    return $maxAgeS !== null && ($now - $anchorTs) <= $maxAgeS;
}

function vehicleSocPercentValue($value) {
    if (is_bool($value) || $value === null || $value === '' || !is_numeric($value)) return null;
    $soc = (float)$value;
    if (!is_finite($soc) || $soc < 0.0 || $soc > 100.0) return null;
    return $soc;
}

function vehicleSocContractFlagActive($value) {
    if ($value === true) return true;
    if (is_bool($value) || $value === null || $value === '') return false;
    if (is_numeric($value)) return (float)$value != 0.0;
    return in_array(strtolower(trim((string)$value)), [
        '1', 'true', 'yes', 'ja', 'on', 'active', 'stale', 'expired', 'invalid',
    ], true);
}

function vehicleSocContractFlagExplicitlyFalse($value) {
    if ($value === false) return true;
    if ($value === null || $value === '') return false;
    if (is_numeric($value)) return (float)$value == 0.0;
    return in_array(strtolower(trim((string)$value)), [
        '0', 'false', 'no', 'nein', 'off', 'unplugged', 'disconnected',
    ], true);
}

function vehicleSocExplicitVetoed($vehicle, $includePlugState = false) {
    if (!is_array($vehicle)) return true;
    if (array_key_exists('soc_rule_confirmed', $vehicle)
        && $vehicle['soc_rule_confirmed'] !== true) return true;
    foreach ([
        'soc_stale', 'stale', 'estimate_expired', 'soc_expired', 'expired',
        'soc_profile_binding_invalid', 'profile_binding_invalid', 'driver_status_stale',
    ] as $key) {
        if (vehicleSocContractFlagActive($vehicle[$key] ?? null)) return true;
    }
    foreach (['driver_status_valid', 'soc_profile_bound'] as $key) {
        if (array_key_exists($key, $vehicle)
            && vehicleSocContractFlagExplicitlyFalse($vehicle[$key])) return true;
    }
    if ($includePlugState) {
        foreach (['plugged', 'is_plugged_in'] as $key) {
            if (array_key_exists($key, $vehicle)
                && vehicleSocContractFlagExplicitlyFalse($vehicle[$key])) return true;
        }
    }
    return false;
}

function wallboxSocPayloadExplicitVetoed($payload) {
    if (!is_array($payload)) return true;
    foreach ([
        'car_soc_stale', 'soc_stale', 'stale', 'estimate_expired',
        'soc_expired', 'expired', 'car_soc_profile_binding_invalid',
        'soc_profile_binding_invalid', 'profile_binding_invalid',
        'driver_status_stale',
    ] as $key) {
        if (vehicleSocContractFlagActive($payload[$key] ?? null)) return true;
    }
    foreach (['driver_status_valid', 'car_soc_profile_bound', 'soc_profile_bound'] as $key) {
        if (array_key_exists($key, $payload)
            && vehicleSocContractFlagExplicitlyFalse($payload[$key])) return true;
    }
    foreach (['plug_state', 'plugged', 'is_plugged_in'] as $key) {
        if (array_key_exists($key, $payload)
            && vehicleSocContractFlagExplicitlyFalse($payload[$key])) return true;
    }
    return false;
}

function vehicleSocDisplayAllowed($vehicle) {
    if (!is_array($vehicle) || vehicleSocPercentValue($vehicle['soc'] ?? null) === null) return false;
    $effectiveSource = strtolower(trim((string)($vehicle['soc_source'] ?? ($vehicle['source'] ?? ''))));
    $originalSource = $effectiveSource === 'vehicle_cached_last_confirmed'
        ? strtolower(trim((string)($vehicle['soc_source_previous'] ?? '')))
        : $effectiveSource;
    if ($originalSource === '' || in_array($originalSource, ['simple_view_start_soc', 'config_start_soc', 'configured_wallbox'], true)) {
        return false;
    }
    if (strpos($originalSource, 'wallbox_estimated_from_simple_view_start_soc') === 0
        || strpos($originalSource, 'wallbox_estimated_from_config_start_soc') === 0) {
        return false;
    }
    return true;
}

function vehicleSocTruthMeta($vehicle, $now = null, $cloudFreshnessS = 900) {
    $now = is_numeric($now) ? (int)$now : time();
    $socValue = vehicleSocPercentValue($vehicle['soc'] ?? null);
    $value = $socValue !== null ? round($socValue, 2) : null;
    $effectiveSource = trim((string)($vehicle['soc_source'] ?? ($vehicle['source'] ?? '')));
    $cached = strtolower($effectiveSource) === 'vehicle_cached_last_confirmed';
    $originalSource = $cached
        ? trim((string)($vehicle['soc_source_previous'] ?? ''))
        : $effectiveSource;
    $sourceTs = vehicleSocRecordTimestamp($vehicle, $now);
    $ageS = $sourceTs !== null ? max(0, $now - $sourceTs) : null;
    $sourceClass = vehicleSocSourceClass($originalSource, $vehicle['is_interpolated'] ?? false);
    $sourceVetoed = vehicleSocExplicitVetoed($vehicle);
    $plugVetoed = !$sourceVetoed && vehicleSocExplicitVetoed($vehicle, true);
    $cloudFreshnessS = is_numeric($cloudFreshnessS)
        ? max(900, min(8 * 3600, (int)$cloudFreshnessS))
        : 900;
    $openwbProAnchored = vehicleSocUsesOpenwbProAnchor($originalSource);
    $mqttAnchored = vehicleSocUsesMqttAnchor($originalSource);
    $trustedSource = wallboxSocSourceTrusted($originalSource);
    $stale = $cached || $sourceVetoed || !$trustedSource;
    $producerAnchorExplicit = array_key_exists('soc_source_ts', $vehicle)
        || array_key_exists('raw_soc_ts', $vehicle);
    $payloadMaxAgeS = e3dcVehicleSocPayloadMaxAgeSeconds(
        $vehicle,
        $originalSource,
        $cloudFreshnessS
    );
    $declaredContractInvalid = e3dcVehicleSocPayloadDeclaresAgeContract($vehicle)
        && $payloadMaxAgeS === null;
    if (($trustedSource && $ageS === null)
        || $declaredContractInvalid
        || ($payloadMaxAgeS !== null && $ageS !== null && $ageS > $payloadMaxAgeS)
        || (($sourceClass === 'cloud' || $mqttAnchored) && !$producerAnchorExplicit)
        || ($openwbProAnchored && $ageS > 8 * 3600)
        || ($mqttAnchored && $ageS > 8 * 3600)
        || ($ageS !== null && (($sourceClass === 'cloud' && $ageS > $cloudFreshnessS)
            || ($sourceClass === 'observed' && !$mqttAnchored && $ageS > 120)))) {
        $stale = true;
    }
    $profileId = trim((string)($vehicle['profile_id'] ?? ''));
    $profileBound = $profileId !== ''
        && (!array_key_exists('soc_profile_bound', $vehicle)
            || !vehicleSocContractFlagExplicitlyFalse($vehicle['soc_profile_bound']));
    $displayUsable = $value !== null
        && vehicleSocDisplayAllowed($vehicle)
        && $trustedSource
        && (!$cached || $sourceTs !== null);
    $explicitProducerRule = ($vehicle['soc_rule_confirmed'] ?? null) === true;
    $ruleUsable = $displayUsable
        && !$stale
        && !$plugVetoed
        && $profileBound
        && (!(($sourceClass === 'cloud') || $mqttAnchored)
            || ($explicitProducerRule && $producerAnchorExplicit))
        && wallboxVehicleSocRuleUsable($vehicle, $cloudFreshnessS);

    return [
        'value' => $displayUsable ? $value : null,
        'profile_id' => $profileId !== '' ? $profileId : null,
        'profile_bound' => $profileBound,
        'source' => $effectiveSource !== '' ? $effectiveSource : null,
        'original_source' => $originalSource !== '' ? $originalSource : null,
        'source_ts' => $sourceTs > 0 ? $sourceTs : null,
        'age_s' => $ageS,
        'class' => $sourceClass,
        'transport_class' => $cached ? 'cached' : $sourceClass,
        'stale' => $stale,
        'display_usable' => $displayUsable,
        'rule_usable' => $ruleUsable,
    ];
}

function resolveWallboxDisplayVehicle($active, $stableIdentityCurrent, $liveName, $chargeProfileName) {
    $liveName = trim((string)$liveName);
    $chargeProfileName = trim((string)$chargeProfileName);
    if (!$active) return ['name' => '', 'source' => '', 'stable_identity_current' => false];
    if ($stableIdentityCurrent && $liveName !== '') {
        return ['name' => $liveName, 'source' => 'openwb_vehicle', 'stable_identity_current' => true];
    }
    if (!$stableIdentityCurrent && $chargeProfileName !== '') {
        return ['name' => $chargeProfileName, 'source' => 'openwb_charge_template', 'stable_identity_current' => false];
    }
    if ($liveName !== '') {
        return ['name' => $liveName, 'source' => 'openwb_vehicle_unstable_fallback', 'stable_identity_current' => false];
    }
    return ['name' => '', 'source' => '', 'stable_identity_current' => false];
}

function vehicleSocDisplayCacheKeys($vehicle) {
    if (!is_array($vehicle)) return [];
    $keys = [];
    foreach (['id', 'profile_id', 'vehicle_id', 'cloud_vehicle_id', 'rfid_tag'] as $field) {
        $compact = compactVehicleIdentifier($vehicle[$field] ?? '');
        if ($compact !== '') $keys[] = $field . ':' . $compact;
    }
    // Namen sind nur Legacyfallback, wenn wirklich keine stabile Bindung
    // existiert. Ein Profilwechsel darf niemals über denselben Anzeigenamen
    // einen alten SoC übernehmen.
    if (empty($keys)) {
        $nameKey = normalizeVehicleMergeKey($vehicle['name'] ?? '');
        if ($nameKey !== '') $keys[] = 'name:' . $nameKey;
    }
    return array_values(array_unique($keys));
}

function loadVehicleSocDisplayCache($file) {
    if (!is_readable($file)) return [];
    $cache = @json_decode(@file_get_contents($file), true);
    return is_array($cache) ? $cache : [];
}

function applyVehicleSocDisplayCache(&$vehicle, $cache, $maxAgeS = 604800) {
    if (!is_array($vehicle) || empty($cache)) return;
    if (vehicleSocPercentValue($vehicle['soc'] ?? null) !== null
        && wallboxVehicleSocRuleUsable($vehicle)) return;

    $now = time();
    foreach (vehicleSocDisplayCacheKeys($vehicle) as $key) {
        if (empty($cache[$key]) || !is_array($cache[$key])) continue;
        $entry = $cache[$key];
        $ts = (int)($entry['ts'] ?? 0);
        if ($ts <= 0 || ($now - $ts) > $maxAgeS) continue;
        $cachedSoc = vehicleSocPercentValue($entry['soc'] ?? null);
        if ($cachedSoc === null) continue;

        $vehicle['soc'] = round($cachedSoc, 2);
        if (vehicleValuePresent($entry['range_km'] ?? null)) {
            $vehicle['range_km'] = (int)round((float)$entry['range_km']);
        }
        $vehicle['soc_source'] = 'vehicle_cached_last_confirmed';
        $vehicle['soc_source_previous'] = $entry['source'] ?? null;
        // Der Cache erhält einen früheren Anzeigewert, bestätigt aber weder
        // eine aktuelle Messung noch die Regelverwendbarkeit.
        $vehicle['soc_rule_confirmed'] = false;
        $vehicle['soc_stale'] = true;
        $vehicle['soc_cache_ts'] = $ts;
        $cacheSourceTs = vehicleSocTimestamp($entry['source_ts'] ?? null, $now);
        $vehicle['soc_source_ts'] = $cacheSourceTs;
        $vehicle['last_updated_at'] = $cacheSourceTs;
        unset($vehicle['raw_soc_ts']);
        return;
    }
}

function saveVehicleSocDisplayCache($file, $vehicles, $existingCache = [], $maxAgeS = 604800) {
    $now = time();
    $cache = is_array($existingCache) ? $existingCache : [];
    foreach ($cache as $key => $entry) {
        $ts = is_array($entry) ? (int)($entry['ts'] ?? 0) : 0;
        if ($ts <= 0 || ($now - $ts) > $maxAgeS) unset($cache[$key]);
    }

    if (is_array($vehicles)) {
        foreach ($vehicles as $vehicle) {
            if (!is_array($vehicle) || !empty($vehicle['soc_stale'])) continue;
            $vehicleSoc = vehicleSocPercentValue($vehicle['soc'] ?? null);
            if ($vehicleSoc === null || !wallboxVehicleSocRuleUsable($vehicle)) continue;
            $sourceTs = vehicleSocRecordTimestamp($vehicle, $now);
            // Ohne echten Quellanker darf selbst ein bestätigter Anzeigewert
            // nicht unter dem aktuellen Cache-Schreibzeitpunkt gespeichert
            // und dadurch scheinbar verjüngt werden.
            if ($sourceTs === null) continue;
            $entry = [
                'soc' => round($vehicleSoc, 2),
                'range_km' => vehicleValuePresent($vehicle['range_km'] ?? null) ? (int)round((float)$vehicle['range_km']) : null,
                'source' => (string)($vehicle['soc_source'] ?? ''),
                'source_ts' => (int)$sourceTs,
                'name' => (string)($vehicle['name'] ?? ''),
                'id' => (string)($vehicle['id'] ?? ''),
                'profile_id' => (string)($vehicle['profile_id'] ?? ''),
                'ts' => $now,
            ];
            foreach (vehicleSocDisplayCacheKeys($vehicle) as $key) {
                $cache[$key] = $entry;
            }
        }
    }

    $tmp = $file . '.tmp';
    if (@file_put_contents($tmp, json_encode($cache), LOCK_EX) !== false) {
        @rename($tmp, $file);
        @chmod($file, 0664);
    }
}

function sanitizeWallboxVehicleSoc(&$vehicle, $cloudFreshnessS = 900) {
    if (!is_array($vehicle)) return;
    $meta = vehicleSocTruthMeta($vehicle, null, $cloudFreshnessS);
    if (empty($meta['display_usable'])) {
        $vehicle['soc'] = null;
        unset($vehicle['range_km']);
    } else {
        $vehicle['soc'] = $meta['value'];
    }
    $vehicle['soc_rule_confirmed'] = !empty($meta['rule_usable']);
    $vehicle['soc_rule_usable'] = !empty($meta['rule_usable']);
    $vehicle['soc_stale'] = !empty($meta['stale']);
    $vehicle['soc_source_original'] = $meta['original_source'];
    $vehicle['soc_source_class'] = $meta['class'];
    $vehicle['soc_source_ts'] = $meta['source_ts'];
    $vehicle['soc_age_s'] = $meta['age_s'];
    $vehicle['soc_profile_bound'] = !empty($meta['profile_bound']);
    $vehicle['soc_meta'] = $meta;
}

function savedCarIdForVehicleIdentifiers($savedCars, $identifiers, $name = '') {
    if (empty($savedCars) || !is_array($savedCars)) return null;
    $probes = [];
    $typed = [
        'profile_id' => ['id', 'profile_id'],
        'car_id' => ['id', 'profile_id'],
        'vehicle_id' => ['vehicle_id', 'vehicle_mac', 'mac'],
        'rfid_tag' => ['rfid', 'rfid_tag'],
        'cloud_vehicle_id' => ['cloud_vehicle_id'],
    ];
    foreach ($identifiers as $type => $id) {
        $probe = compactVehicleIdentifier($id ?? '');
        if ($probe === '') continue;
        $probeType = is_string($type) && isset($typed[$type]) ? $type : 'legacy_any';
        if (!isset($probes[$probeType])) $probes[$probeType] = [];
        $probes[$probeType][] = $probe;
    }
    $name = normalizeVehicleMergeKey($name);

    foreach ($savedCars as $car) {
        if (!is_array($car)) continue;
        foreach ($probes as $type => $values) {
            $fields = $type === 'legacy_any'
                ? ['id', 'profile_id', 'vehicle_id', 'vehicle_mac', 'mac', 'rfid', 'rfid_tag', 'cloud_vehicle_id']
                : $typed[$type];
            foreach ($fields as $key) {
                $saved = compactVehicleIdentifier($car[$key] ?? '');
                if ($saved !== '' && in_array($saved, $values, true)) return $car['id'] ?? null;
            }
        }
    }
    if (!empty($probes)) return null;
    if ($name !== '') {
        $nameMatches = [];
        foreach ($savedCars as $car) {
            if (!is_array($car)) continue;
            $savedName = normalizeVehicleMergeKey($car['name'] ?? '');
            if ($savedName !== '' && $name === $savedName) {
                $savedId = trim((string)($car['id'] ?? ''));
                if ($savedId !== '') $nameMatches[$savedId] = true;
            }
        }
        if (count($nameMatches) === 1) {
            foreach ($nameMatches as $savedId => $_unused) return $savedId;
        }
    }
    return null;
}

function ambiguousSavedCarNameKeys($savedCars) {
    if (!is_array($savedCars)) return [];
    $idsByName = [];
    foreach ($savedCars as $car) {
        if (!is_array($car)) continue;
        $nameKey = normalizeVehicleMergeKey($car['name'] ?? '');
        $savedId = trim((string)($car['id'] ?? ''));
        if ($nameKey === '' || $savedId === '') continue;
        if (!isset($idsByName[$nameKey])) $idsByName[$nameKey] = [];
        $idsByName[$nameKey][$savedId] = true;
    }
    $ambiguous = [];
    foreach ($idsByName as $nameKey => $ids) {
        if (count($ids) > 1) $ambiguous[] = $nameKey;
    }
    return $ambiguous;
}

function savedCarProfileIdForVehicleRecord($veh, $savedCars) {
    if (!is_array($veh) || empty($savedCars) || !is_array($savedCars)) return null;
    $recordId = trim((string)($veh['id'] ?? ''));
    if (in_array($recordId, ['openwb_native', 'openwb_pro_wb2'], true) || strpos($recordId, 'vehicle_') === 0) {
        $recordId = '';
    }
    return savedCarIdForVehicleIdentifiers(
        $savedCars,
        [
            'profile_id' => $veh['profile_id'] ?? $recordId,
            'car_id' => empty($veh['profile_id']) ? $recordId : '',
            'vehicle_id' => $veh['vehicle_id'] ?? '',
            'rfid_tag' => $veh['rfid_tag'] ?? '',
            'cloud_vehicle_id' => $veh['cloud_vehicle_id'] ?? '',
        ],
        $veh['name'] ?? ''
    );
}

function savedCarForVehicleIdentifiers($savedCars, $identifiers, $name = '') {
    $profileId = savedCarIdForVehicleIdentifiers($savedCars, $identifiers, $name);
    if ($profileId === null) return null;
    foreach ($savedCars as $car) {
        if (is_array($car) && ($car['id'] ?? null) === $profileId) return $car;
    }
    return null;
}

function savedCarForWallboxSelection($savedCars, $selection) {
    $selection = trim((string)$selection);
    if ($selection === '' || $selection === '__none' || empty($savedCars) || !is_array($savedCars)) return null;
    $profileId = savedCarIdForVehicleIdentifiers($savedCars, ['profile_id' => $selection], '');
    if ($profileId === null) $profileId = savedCarIdForVehicleIdentifiers($savedCars, [], $selection);
    foreach ($savedCars as $car) {
        if (!is_array($car)) continue;
        if ($profileId !== null && ($car['id'] ?? null) === $profileId) return $car;
        foreach (['id', 'vehicle_id', 'vehicle_mac', 'mac', 'rfid', 'rfid_tag', 'cloud_vehicle_id'] as $key) {
            if (compactVehicleIdentifier($car[$key] ?? '') !== '' && compactVehicleIdentifier($car[$key] ?? '') === compactVehicleIdentifier($selection)) {
                return $car;
            }
        }
    }
    return null;
}

function wallboxSlotLooksConnected($data, $slot) {
    $slot = (int)$slot;
    if ($slot === 2) {
        return !empty($data['wb2_plug'])
            || !empty($data['wb2_locked'])
            || !empty($data['wb2_charging'])
            || abs((float)($data['wb2'] ?? 0)) > 50;
    }
    return !empty($data['wb_plug'])
        || !empty($data['wb_locked'])
        || !empty($data['wb_charging'])
        || abs((float)($data['wb'] ?? 0)) > 50;
}

function wallboxSlotHasOwnVehicleIdentity($data, $slot) {
    foreach (wallboxSlotIdentityPrefixes($slot) as $prefix) {
        foreach (["{$prefix}_car_name", "{$prefix}_car_id", "{$prefix}_vehicle_id", "{$prefix}_rfid_tag"] as $key) {
            if (!empty($data[$key])) return true;
        }
    }
    return false;
}

function wallboxSlotIdentityPrefixes($slot) {
    return ((int)$slot === 2) ? ['wb2'] : ['wb', 'wb1'];
}

function wallboxCompactValues($source, $keys) {
    $values = [];
    if (!is_array($source)) return $values;
    foreach ($keys as $key) {
        $compact = compactVehicleIdentifier($source[$key] ?? '');
        if ($compact !== '') $values[] = $compact;
    }
    return array_values(array_unique($values));
}

function wallboxSlotLiveVehicleIdentifiers($data, $slot) {
    $values = [];
    foreach (wallboxSlotIdentityPrefixes($slot) as $prefix) {
        foreach (["{$prefix}_vehicle_id", "{$prefix}_rfid_tag"] as $key) {
            $compact = compactVehicleIdentifier($data[$key] ?? '');
            if ($compact !== '') $values[] = $compact;
        }
    }
    return array_values(array_unique($values));
}

function configuredWallboxVehicleAlreadyLiveOnOtherSlot($data, $car, $slot) {
    $otherSlot = ((int)$slot === 2) ? 1 : 2;
    if (!wallboxSlotLooksConnected($data, $otherSlot)) return false;

    $otherLiveIds = wallboxSlotLiveVehicleIdentifiers($data, $otherSlot);
    if (empty($otherLiveIds)) return false;

    $selectedIds = wallboxCompactValues($car, ['id', 'profile_id', 'vehicle_id', 'vehicle_mac', 'mac', 'rfid', 'rfid_tag', 'cloud_vehicle_id']);
    foreach ($selectedIds as $selectedId) {
        if (in_array($selectedId, $otherLiveIds, true)) return true;
    }
    return false;
}

function injectConfiguredWallboxVehicle(&$data, $savedCars, $conf, $slot) {
    $slot = (int)$slot;
    if (!wallboxSlotLooksConnected($data, $slot) || wallboxSlotHasOwnVehicleIdentity($data, $slot)) return;
    $selectionKey = ($slot === 2) ? 'wb2_car_id' : 'wb1_car_id';
    $car = savedCarForWallboxSelection($savedCars, $conf[$selectionKey] ?? '');
    if (!$car) return;
    if (configuredWallboxVehicleAlreadyLiveOnOtherSlot($data, $car, $slot)) return;

    $profileId = $car['id'] ?? null;
    $name = trim((string)($car['name'] ?? ''));
    $prefix = ($slot === 2) ? 'wb2' : 'wb';
    $aliasPrefix = ($slot === 2) ? 'wb2' : 'wb1';
    $charging = ($slot === 2)
        ? (!empty($data['wb2_charging']) || abs((float)($data['wb2'] ?? 0)) > 50)
        : (!empty($data['wb_charging']) || abs((float)($data['wb'] ?? 0)) > 50);

    $data[$prefix . '_car_name'] = $name;
    $data[$prefix . '_car_id'] = $profileId;
    $data[$prefix . '_vehicle_id'] = $car['vehicle_id'] ?? null;
    $data[$prefix . '_rfid_tag'] = $car['rfid'] ?? ($car['rfid_tag'] ?? null);
    $data[$aliasPrefix . '_car_name'] = $name;
    $data[$aliasPrefix . '_car_id'] = $profileId;
    $data[$aliasPrefix . '_vehicle_id'] = $car['vehicle_id'] ?? null;
    $data[$aliasPrefix . '_rfid_tag'] = $car['rfid'] ?? ($car['rfid_tag'] ?? null);

    if (!isset($data['vehicles']) || !is_array($data['vehicles'])) $data['vehicles'] = [];
    $data['vehicles'][] = [
        'id' => $profileId,
        'profile_id' => $profileId,
        'name' => $name,
        'soc' => null,
        'soc_rule_confirmed' => false,
        'capacity_kwh' => isset($car['capacity']) ? (float)$car['capacity'] : null,
        'power' => isset($car['power']) ? (float)$car['power'] : null,
        'target_soc' => $car['target_soc'] ?? null,
        'max_soc' => $car['max_soc'] ?? null,
        'cloud_vehicle_id' => $car['cloud_vehicle_id'] ?? null,
        'vehicle_id' => $car['vehicle_id'] ?? null,
        'rfid_tag' => $car['rfid'] ?? ($car['rfid_tag'] ?? null),
        'is_plugged_in' => true,
        'is_charging' => $charging,
        'wb_slot' => $slot,
        'soc_source' => 'configured_wallbox',
        'last_updated_at' => time()
    ];
}

function vehicleValuePresent($value) {
    if ($value === null || $value === '') return false;
    if (is_numeric($value) && (float)$value == 0.0) return false;
    return true;
}

function liveBoolValue($value, $default = false) {
    if (is_bool($value)) return $value;
    if ($value === null || $value === '') return $default;
    if (is_numeric($value)) return ((float)$value) != 0.0;
    $s = strtolower(trim((string)$value));
    if (in_array($s, ['1', 'true', 'yes', 'on', 'locked', 'lock', 'closed', 'connected'], true)) return true;
    if (in_array($s, ['0', 'false', 'no', 'off', 'unlocked', 'open', 'opened', 'disconnected'], true)) return false;
    return $default;
}

function liveNativeWallboxStatusContract($detail) {
    if (!is_array($detail)) {
        return ['declared' => false, 'valid' => null, 'source' => '', 'reason' => ''];
    }
    $declared = array_key_exists('wb_status_valid', $detail)
        || array_key_exists('wb_status_source', $detail)
        || array_key_exists('wb_status_reason', $detail)
        || array_key_exists('car_connected_rscp', $detail);
    if (!$declared) {
        return ['declared' => false, 'valid' => null, 'source' => '', 'reason' => ''];
    }
    $valid = (($detail['wb_status_valid'] ?? null) === true)
        && (!array_key_exists('driver_status_valid', $detail)
            || (($detail['driver_status_valid'] ?? null) === true))
        && empty($detail['driver_status_stale'])
        && empty($detail['driver_status_degraded'])
        && empty($detail['driver_status_glitch'])
        && (($detail['driver_status_plausible'] ?? null) !== false)
        && (($detail['valid'] ?? null) !== false)
        && empty($detail['stale']);
    $reason = trim((string)($detail['wb_status_reason'] ?? ''));
    if (!$valid) {
        foreach (['driver_status_glitch_reason', 'driver_status_reason', 'wb_status_reason'] as $reasonKey) {
            $candidate = trim((string)($detail[$reasonKey] ?? ''));
            if ($candidate !== '' && !in_array(strtolower($candidate), ['ok', 'fresh'], true)) {
                $reason = $candidate;
                break;
            }
        }
        if ($reason === '' || in_array(strtolower($reason), ['ok', 'fresh'], true)) {
            $reason = 'native_status_not_fresh';
        }
    } elseif ($reason === '') {
        $reason = 'fresh';
    }
    return [
        'declared' => true,
        'valid' => $valid,
        'source' => (string)($detail['wb_status_source'] ?? 'native_status_contract'),
        'reason' => $reason,
    ];
}

function liveSetExactCounter(&$data, $key, $value, $source, $priority = 0) {
    if ($value === null || !is_numeric($value)) return;
    $val = round((float)$value, 3);
    if ($val < 0 || $val >= 2000) return;

    $prioKey = $key . '_priority';
    $srcKey = $key . '_source';
    $oldPrio = isset($data[$prioKey]) ? (int)$data[$prioKey] : -1;
    $oldVal = isset($data[$key]) && is_numeric($data[$key]) ? (float)$data[$key] : null;

    if (!array_key_exists($key, $data) || $priority >= $oldPrio || (($oldVal === null || $oldVal <= 0.0) && $val > 0.0)) {
        $data[$key] = $val;
        $data[$prioKey] = (int)$priority;
        $data[$srcKey] = (string)$source;
    }
}

function liveWallboxClosedSessionsTodayKwh($csvFile, $wallboxNo) {
    $target = (string)((int)$wallboxNo);
    $aggregate = e3dcWallboxSessionCsvAggregate($csvFile);
    $today = is_array($aggregate['today_kwh'] ?? null)
        ? $aggregate['today_kwh']
        : [];
    return round(max(0.0, (float)($today[$target] ?? 0.0)), 3);
}

function wallboxPayloadSlotMatches($payload, $expectedWallbox, $allowMissing = true) {
    if (!is_array($payload)) return false;
    $slotKey = array_key_exists('wb_slot', $payload)
        ? 'wb_slot'
        : (array_key_exists('wb', $payload) ? 'wb' : null);
    if ($slotKey === null || $payload[$slotKey] === null || $payload[$slotKey] === '') {
        return $allowMissing === true;
    }
    $rawSlot = $payload[$slotKey];
    if (is_bool($rawSlot) || !is_numeric($rawSlot)) return false;
    return (int)$rawSlot === (int)$expectedWallbox;
}

function liveCarChargeSessionSnapshot($wallboxNo) {
    static $requestMemo = [];

    $wallbox = max(1, (int)$wallboxNo);
    if (array_key_exists($wallbox, $requestMemo)) {
        return $requestMemo[$wallbox];
    }
    $liveName = $wallbox === 1
        ? 'car_charge_session.json'
        : 'car_charge_session_wb' . $wallbox . '.json';
    $candidates = [
        [
            'source' => 'ramdisk',
            'path' => '/var/www/html/ramdisk/' . $liveName,
            'max_age_s' => 300.0,
        ],
        [
            'source' => 'checkpoint',
            'path' => '/var/www/html/data/car_charge_session_checkpoint_wb' . $wallbox . '.json',
            'max_age_s' => 36 * 3600.0,
        ],
        [
            'source' => 'legacy',
            'path' => '/var/www/html/tmp/' . $liveName,
            'max_age_s' => 36 * 3600.0,
        ],
    ];
    foreach ($candidates as $candidate) {
        $path = e3dcFirstFreshRegularFile(
            [$candidate['path']],
            $candidate['max_age_s']
        );
        if (!is_string($path)) continue;
        $raw = e3dcReadRegularFileBound($path, 1024 * 1024);
        $payload = is_string($raw) ? @json_decode($raw, true) : null;
        if (is_array($payload)
            && wallboxPayloadSlotMatches($payload, $wallbox, true)) {
            $requestMemo[$wallbox] = [
                'data' => $payload,
                'source' => $candidate['source'],
            ];
            return $requestMemo[$wallbox];
        }
    }
    $requestMemo[$wallbox] = ['data' => [], 'source' => 'none'];
    return $requestMemo[$wallbox];
}

function explicitVehicleRangeContract($vehicle, $nowTs = null) {
    if (!is_array($vehicle)) return null;

    $now = is_numeric($nowTs) ? (float)$nowTs : (float)time();
    $source = strtolower(trim((string)($vehicle['car_range_source'] ?? '')));
    $range = $vehicle['range_km'] ?? null;
    if (!in_array($source, ['http_total', 'mqtt_total'], true)
        || ($vehicle['car_range_valid'] ?? false) !== true
        || !is_numeric($range)
        || (float)$range <= 0.0) {
        return null;
    }

    $observedTs = $vehicle['car_range_observed_ts'] ?? null;
    if (!is_numeric($observedTs)) return null;
    $observedTs = (float)$observedTs;
    $observedAge = $now - $observedTs;
    if ($observedTs <= 0.0 || $observedAge < -5.0 || $observedAge > 120.0) {
        return null;
    }

    $sourceTsExplicit = ($vehicle['car_range_source_ts_explicit'] ?? false) === true;
    $sourceTs = $vehicle['car_range_source_ts'] ?? null;
    if ($sourceTsExplicit) {
        if (!is_numeric($sourceTs)) return null;
        $sourceTs = (float)$sourceTs;
        $sourceAge = $now - $sourceTs;
        if ($sourceTs <= 0.0 || $sourceAge < -5.0 || $sourceAge > 8 * 3600.0) {
            return null;
        }
    } else {
        $sourceTs = $observedTs;
    }

    $vehicleKey = trim((string)($vehicle['car_range_vehicle_key'] ?? ''));
    if ($vehicleKey !== '') {
        if (($vehicle['stable_vehicle_identity_current'] ?? false) !== true) {
            return null;
        }
        $currentVehicleKeys = [];
        foreach (['vehicle_id', 'rfid_tag', 'car_id'] as $key) {
            $value = trim((string)($vehicle[$key] ?? ''));
            if ($value !== '') $currentVehicleKeys[] = $value;
        }
        if (!$currentVehicleKeys || !in_array($vehicleKey, $currentVehicleKeys, true)) {
            return null;
        }
    }

    return [
        'range_km' => (float)$range,
        'car_range_source' => $source,
        'car_range_valid' => true,
        'car_range_observed_ts' => (int)$observedTs,
        'car_range_source_ts' => (int)$sourceTs,
        'car_range_source_ts_explicit' => $sourceTsExplicit,
        'car_range_vehicle_key' => $vehicleKey,
    ];
}

function mergeVehicleRecords($base, $incoming, $cloudFreshnessS = 900) {
    $merged = is_array($base) ? $base : [];
    $incoming = is_array($incoming) ? $incoming : [];
    // SoC und Gesamtreichweite besitzen absichtlich getrennte Quellen.
    // Ein neuer Cloud-SoC ohne Reichweite darf eine frische, identitätsgebundene
    // openWB-Gesamtreichweite nicht aus dem Fahrzeugdatensatz löschen.
    $baseRange = explicitVehicleRangeContract($merged);
    $incomingRange = explicitVehicleRangeContract($incoming);

    // Das Ladeziel ist ein eigener Vertrag: target_soc wird nur ergänzt und
    // niemals zusammen mit einem gewinnenden oder verlierenden Ist-SoC bewegt.
    foreach (['capacity', 'capacity_kwh', 'power', 'charge_power', 'charge_power_kw', 'target_soc', 'max_soc', 'max_soc_si'] as $key) {
        if (!vehicleValuePresent($merged[$key] ?? null) && vehicleValuePresent($incoming[$key] ?? null)) {
            $merged[$key] = $incoming[$key];
        }
    }

    $incomingPriority = vehicleSocSourcePriority(
        $incoming['soc_source'] ?? '',
        $incoming['is_interpolated'] ?? false,
        vehicleSocRecordTimestamp($incoming),
        $cloudFreshnessS
    );
    $basePriority = vehicleSocSourcePriority(
        $merged['soc_source'] ?? '',
        $merged['is_interpolated'] ?? false,
        vehicleSocRecordTimestamp($merged),
        $cloudFreshnessS
    );
    $incomingHasSoc = vehicleSocPercentValue($incoming['soc'] ?? null) !== null;
    $baseHasSoc = vehicleSocPercentValue($merged['soc'] ?? null) !== null;
    $incomingSocWins = $incomingHasSoc && !$baseHasSoc;
    if ($incomingHasSoc && $baseHasSoc) {
        $mergeTruthRank = static function ($vehicle) use ($cloudFreshnessS) {
            $probe = is_array($vehicle) ? $vehicle : [];
            if (trim((string)($probe['profile_id'] ?? '')) === '') {
                $probe['profile_id'] = '__merge_identity__';
            }
            $meta = vehicleSocTruthMeta($probe, null, $cloudFreshnessS);
            if (!empty($meta['rule_usable'])) return 2;
            return !empty($meta['display_usable']) ? 1 : 0;
        };
        $incomingTruthRank = $mergeTruthRank($incoming);
        $baseTruthRank = $mergeTruthRank($merged);
        if ($incomingTruthRank !== $baseTruthRank) {
            $incomingSocWins = $incomingTruthRank > $baseTruthRank;
        } elseif ($incomingPriority > $basePriority) {
            $incomingSocWins = true;
        } elseif ($incomingPriority === $basePriority) {
            // Bei gleicher fachlicher Quellenpriorität entscheidet ausschließlich
            // der belastbare Quellzeitpunkt. Ohne neueren Beleg bleibt der bereits
            // gebundene Ist-SoC erhalten; die Einmischreihenfolge ist keine Wahrheit.
            $incomingTs = vehicleSocRecordTimestamp($incoming);
            $baseTs = vehicleSocRecordTimestamp($merged);
            $incomingSocWins = $incomingTs !== null
                && ($baseTs === null || $incomingTs > $baseTs);
        }
    }
    $baseSocSource = strtolower(trim((string)($merged['soc_source'] ?? '')));
    $incomingSocSource = strtolower(trim((string)($incoming['soc_source'] ?? '')));
    if (in_array($baseSocSource, ['openwb_pro_raw', 'openwb_pro_estimated'], true)
        && strpos($incomingSocSource, 'wallbox_estimated_from_') === 0) {
        $incomingSocWins = false;
    }
    $baseIsProfileFallback = !empty($merged['soc_profile_bound'])
        || strpos($baseSocSource, 'wallbox_estimated') === 0;
    if (vehicleSocContractFlagActive($incoming['soc_profile_binding_invalid'] ?? null)) {
        $incomingSocWins = false;
        if (vehicleSocPercentValue($merged['soc'] ?? null) === null || $baseIsProfileFallback) {
            $merged['soc'] = null;
            unset($merged['range_km']);
            $merged['soc_source'] = 'wallbox_estimated_profile_binding_invalid';
            $merged['soc_rule_confirmed'] = false;
            $merged['soc_profile_binding_invalid'] = true;
        }
    }
    if ($incomingSocWins) {
        foreach ([
            'soc', 'range_km', 'soc_source', 'soc_source_previous',
            'soc_rule_confirmed', 'soc_rule_usable', 'soc_stale', 'stale',
            'estimate_expired', 'soc_expired', 'expired',
            'soc_profile_binding_invalid', 'profile_binding_invalid',
            'soc_cache_ts', 'soc_source_ts', 'raw_soc_ts', 'is_interpolated',
            'soc_age_contract', 'soc_age_contract_source', 'soc_max_age_s',
            'driver_status_stale', 'driver_status_valid', 'soc_profile_bound',
            'last_updated_at',
        ] as $key) {
            if (array_key_exists($key, $incoming)) {
                $merged[$key] = $incoming[$key];
            } else {
                unset($merged[$key]);
            }
        }
        if (!vehicleSocContractFlagActive($incoming['soc_profile_binding_invalid'] ?? null)) {
            unset($merged['soc_profile_binding_invalid']);
        }
    }
    $basePlugged = liveBoolValue($merged['is_plugged_in'] ?? false) || liveBoolValue($merged['is_charging'] ?? false);
    $incomingPlugged = liveBoolValue($incoming['is_plugged_in'] ?? false) || liveBoolValue($incoming['is_charging'] ?? false);
    foreach (['is_plugged_in', 'is_charging'] as $key) {
        if (array_key_exists($key, $incoming)) {
            $incomingBool = liveBoolValue($incoming[$key], false);
            if ($incomingBool || !array_key_exists($key, $merged) || !$basePlugged) {
                $merged[$key] = $incomingBool;
            }
        }
    }
    if (vehicleValuePresent($incoming['time_to_target_mins'] ?? null)) {
        $merged['time_to_target_mins'] = $incoming['time_to_target_mins'];
    }
    if (vehicleValuePresent($incoming['wb_slot'] ?? null)
        && (!vehicleValuePresent($merged['wb_slot'] ?? null) || $incomingPlugged || !$basePlugged)) {
        $merged['wb_slot'] = $incoming['wb_slot'];
    }
    foreach (['rfid_tag', 'vehicle_id', 'car_id', 'cloud_vehicle_id', 'profile_id'] as $key) {
        if (vehicleValuePresent($incoming[$key] ?? null) && !vehicleValuePresent($merged[$key] ?? null)) {
            $merged[$key] = $incoming[$key];
        }
    }
    if (($incoming['stable_vehicle_identity_current'] ?? false) === true) {
        $merged['stable_vehicle_identity_current'] = true;
    }

    if (empty($merged['name']) && !empty($incoming['name'])) {
        $merged['name'] = $incoming['name'];
    }
    if (empty($merged['id']) && !empty($incoming['id'])) {
        $merged['id'] = $incoming['id'];
    }

    $selectedRange = null;
    if (is_array($baseRange) && is_array($incomingRange)) {
        $selectedRange = (int)$incomingRange['car_range_observed_ts']
            >= (int)$baseRange['car_range_observed_ts']
            ? $incomingRange
            : $baseRange;
    } elseif (is_array($incomingRange)) {
        $selectedRange = $incomingRange;
    } elseif (is_array($baseRange)) {
        $selectedRange = $baseRange;
    }
    if (is_array($selectedRange)) {
        foreach ($selectedRange as $key => $value) {
            $merged[$key] = $value;
        }
    } else {
        foreach ([
            'car_range_source',
            'car_range_valid',
            'car_range_observed_ts',
            'car_range_source_ts',
            'car_range_source_ts_explicit',
            'car_range_vehicle_key',
        ] as $key) {
            unset($merged[$key]);
        }
    }

    return $merged;
}

$liveHistoryFile = '/var/www/html/ramdisk/live_history.txt';
$liveHistoryHours = 48;
$data = [
    'pv' => 0,
    'pv_total_w' => 0,
    'pv_e3dc_w' => 0,
    'pv_external_w' => 0,
    'pv_external_source' => 'not_reported',
    'pv_external_power_valid' => false,
    'pv_external_power_age_s' => null,
    'pv_external_capable' => false,
    'pv_external_topology_present' => false,
    'pv_external_topology_valid' => false,
    'pv_external_topology_source' => 'none',
    'pv_external_topology_evidence_state' => 'unknown',
    'pv_external_topology_reason' => 'not_confirmed',
    'pv_external_control_available' => false,
    'pv_external_direct_energy_valid' => false,
    'pv_external_direct_energy_source' => 'unverified',
    'pv_dc_only_configured' => false,
    'pv_dc_only_active' => false,
    'pv_external_charge_locked' => false,
    'pv_external_charge_guard_w' => 0,
    'pv_external_charge_lock_reason' => '',
    'bat' => 0,
    'home_raw' => 0,
    'home_rscp_raw' => 0,
    'home_balance' => null,
    'home_delta' => null,
    'home_power_source' => 'legacy_unmarked',
    'home_power_valid' => true,
    'home_power_independent' => true,
    'grid_power_valid' => true,
    'rscp_sample_valid' => true,
    'rscp_glitch_reasons' => [],
    'notstrom_status' => 0,
    'hs_power' => -1,
    'grid' => 0,
    'soc' => 0,
    'pv_today_kwh' => null,
    'wb' => 0,
    'wb_session_kwh' => null,
    'wp' => 0,
    'climate_power_w' => 0,
    'climate_active' => false,
    'climate_daily_kwh' => null,
    'climate_source' => '',
    'climate_name' => '',
    'climate_phase' => '',
    'climate_online' => false,
    'wp_boost_active' => false,
    'wp_price_boost' => false,
    'wp_market_plan' => false,
    'wp_predump_boost' => false,
    'wp_manual_boost' => false,
    'wp_boost_owner' => 'none',
    'wp_pause_active' => false,
    'wp_pre_pause_active' => false,
    'wp_sg_ready_active' => null,
    'wp_sg_ready_valid' => false,
    'wp_sg_ready_state' => 'unavailable',
    'wp_sg_ready_source' => 'unavailable',
    'wp_sg_ready_label' => '',
    'wp_sg_ready_age_s' => null,
    'mb_state' => 'IDLE',
    'mb_prio' => '',
    'heat_manager_active' => false,
    'heat_manager_label' => 'Beobachtet',
    'heat_manager_owner_key' => 'none',
    'heat_manager_owner_label' => 'Beobachtet',
    'heat_manager_owner_kind' => 'observe',
    'heat_manager_owner_reason' => 'Kein aktiver Wärmeauftrag; Energy Manager beobachtet nur.',
    'heat_manager_reason' => '',
    'heat_manager_storage_state' => '',
    'heatpump_budget_w' => null,
    'wp_ww_temp' => null,
    'wp_mode' => null,
    'wp_live_status' => 'not_configured',
    'wp_live_fresh' => false,
    'wp_live_age_s' => null,
    'wp_live_source' => '',
    'wp_live_error' => '',
    'wp_rl_source' => 'internal', // Default
    'wp_rl_temp' => null,
    'wp_rl_soll' => null,
    'wp_vl_temp' => null,
    'wp_vl_soll' => null,
    'wp_kaelte_temp' => null,
    'wp_kaelte_soll' => null,
    'wp_zuluft_temp' => null,
    'wp_sole_ein_temp' => null,
    'wp_sole_aus_temp' => null,
    'wp_ww_soll' => null,
    'wp_heat_kw' => null,
    'wp_electric_w' => null,
    'dc0_w' => 0, 'dc0_v' => 0, 'dc0_a' => 0,
    'dc1_w' => 0, 'dc1_v' => 0, 'dc1_a' => 0,
    'ac0_w' => 0, 'ac0_v' => 0, 'ac0_a' => 0,
    'ac1_w' => 0, 'ac1_v' => 0, 'ac1_a' => 0,
    'ac2_w' => 0, 'ac2_v' => 0, 'ac2_a' => 0,
    'wb_p1' => 0, 'wb_p2' => 0, 'wb_p3' => 0,
    'wb2_p1' => 0, 'wb2_p2' => 0, 'wb2_p3' => 0,
    'grid_p1' => null, 'grid_p2' => null, 'grid_p3' => null,
    'grid_pm_available' => false,
    'grid_pm_index' => null,
    'grid_pm_source' => '',
    'pvi_frequency_hz' => null,
    'pvi_frequency_valid' => false,
    'pvi_frequency_source' => 'unavailable',
    'grid_frequency_hz' => null,
    'grid_frequency_valid' => false,
    'grid_frequency_source' => 'unavailable',
    'grid_frequency_age_s' => null,
    'bat_v' => 0, 'bat_a' => 0,
    'bat1_v' => 0, 'bat1_a' => 0,
    'wb_status' => '',
    'wb_locked' => null,
    'wb_plug' => null,
    'wb_charging' => null,
    'wb_status_valid' => false,
    'wb_status_source' => 'unknown',
    'wb_status_reason' => 'missing',
    'wb_phases' => 0,
    'detected_phases' => 0,
    'connected' => false,
    'charging_active' => false,
    'wb_mode' => 0,
    'car_force_running' => false,
    'price_ct' => null,
    'price_level' => 'unknown',
    'price_slot_gmt' => null,
    'price_target_slot_gmt' => null,
    'price_source' => null,
    'price_resolution_min' => null,
    'price_source_resolution_min' => null,
    'price_min_ct' => null,
    'price_min_slot' => null,
    'price_max_ct' => null,
    'price_max_slot' => null,
    'cheap_grid_boost_enabled' => false,
    'cheap_grid_boost_active' => false,
    'cheap_grid_boost_window' => null,
    'cheap_grid_boost_next_window' => null,
    'cheap_grid_boost_allow' => [],
    'cheap_grid_charge' => null,
    'direct_marketing' => null,
    'direct_marketing_monitor' => null,
    'direct_marketing_daily_report' => null,
    'direct_marketing_aux_inverter_shelly' => null,
    'market_value_solar' => null,
    'prices' => [],
    'price_start_hour' => null,
    'price_interval' => 1.0,
    'forecast' => [],
    'weather_alert' => null,
    'time' => '--:--',
    'cpu_load' => 0,
    'cpu_temp' => null,
    'ts' => 0,
    'notstrom_reserve' => 0
];

$directNativeWbStatusInvalid = false;

$paths = getInstallPaths();

// Config laden via helpers.php Funktion
$confData = loadE3dcConfig();
$wallboxConfig = $confData['config'] ?? [];
$wbConfigured = hasWallbox1Config($wallboxConfig);
$wb2Configured = hasWallbox2Config($wallboxConfig);
$wb2ExplicitlyDisabled = isWallbox2ExplicitlyDisabledConfig($wallboxConfig);
$auxInverterCfg = $wallboxConfig;
$directMarketingConfigured = in_array(
    strtolower(trim((string)($auxInverterCfg['direct_marketing_enable'] ?? '0'))),
    ['1', 'true', 'yes', 'on'],
    true
);
$data['direct_marketing_enabled'] = $directMarketingConfigured;

function liveTrajectoryCanonicalize($value) {
    if (!is_array($value)) return $value;
    $keys = array_keys($value);
    $isList = count($value) === 0 || $keys === range(0, count($value) - 1);
    if ($isList) return array_map('liveTrajectoryCanonicalize', $value);
    ksort($value, SORT_STRING);
    foreach ($value as $key => $item) $value[$key] = liveTrajectoryCanonicalize($item);
    return $value;
}

function liveTrajectoryCanonicalJson($value) {
    return json_encode(
        liveTrajectoryCanonicalize($value),
        JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_PRESERVE_ZERO_FRACTION
    );
}


function livePlanCanonicalizePreservingObjects($value) {
    if (is_object($value)) {
        $items = get_object_vars($value);
        ksort($items, SORT_STRING);
        $result = new stdClass();
        foreach ($items as $key => $item) {
            $result->{$key} = livePlanCanonicalizePreservingObjects($item);
        }
        return $result;
    }
    if (is_array($value)) {
        return array_map('livePlanCanonicalizePreservingObjects', $value);
    }
    return $value;
}

function livePlanCanonicalJsonPreservingObjects($value) {
    return json_encode(
        livePlanCanonicalizePreservingObjects($value),
        JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_PRESERVE_ZERO_FRACTION
    );
}

function liveReadStoragePlanActionProjectionArtifact($path, $maxBytes = 524288) {
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

function liveDirectMarketingActionBindingValid($plan, $projection, $action, $plannedW, $slotStart, $slotEnd, $validFrom, $horizonEnd) {
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
        if (liveTrajectoryCanonicalJson($window['export_segment_id'] ?? null)
            !== liveTrajectoryCanonicalJson($projection['direct_marketing_export_segment_id'] ?? null)) {
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
        $businessEncoded = liveTrajectoryCanonicalJson($businessMaterial);
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
        $gateEncoded = liveTrajectoryCanonicalJson($startGate);
        $gateSha = is_string($gateEncoded) ? 'sha256:' . hash('sha256', $gateEncoded) : '';
        $lineageMaterial = [
            'schema' => 'export_window_gate_lineage_v1',
            'gate_sha256' => $gateSha,
            'action' => $sourceAction,
            'window_id' => $windowId,
            'origin_start_ts' => $windowStart,
            'end_ts' => $windowEnd,
        ];
        $lineageEncoded = liveTrajectoryCanonicalJson($lineageMaterial);
        $expectedLineageId = is_string($lineageEncoded)
            ? 'sha256:' . hash('sha256', $lineageEncoded) : '';
        $generation = $lineage['current_generation'] ?? null;
        if (!is_int($generation) || $generation < 1) return false;
        $generationEncoded = liveTrajectoryCanonicalJson([
            'gate_lineage_id' => $expectedLineageId,
            'generation' => $generation,
        ]);
        $expectedGenerationId = is_string($generationEncoded)
            ? 'sha256:' . hash('sha256', $generationEncoded) : '';
        $expectedPreviousId = null;
        if ($generation > 1) {
            $previousEncoded = liveTrajectoryCanonicalJson([
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
            || liveTrajectoryCanonicalJson($exportGate['action_horizon_contract'] ?? null)
                !== liveTrajectoryCanonicalJson($horizon)) {
            return false;
        }
        $identityMaterial['gate_lineage_id'] = $expectedLineageId;
        $identityMaterial['gate_generation'] = $generation;
        $identityMaterial['gate_generation_id'] = $expectedGenerationId;
    }
    $identityEncoded = liveTrajectoryCanonicalJson($identityMaterial);
    $expectedActionId = is_string($identityEncoded)
        ? 'sha256:' . hash('sha256', $identityEncoded) : '';
    return $expectedActionId !== '' && hash_equals($expectedActionId, $actionId);
}

function liveHeadroomProjectionEvidence($plan) {
    $invalid = static function($reason) {
        return ['valid' => false, 'reason' => $reason, 'present' => true, 'revision' => null, 'slots_by_bounds' => [], 'groups_by_id' => []];
    };
    $direct = is_array($plan['direct_marketing'] ?? null) ? $plan['direct_marketing'] : [];
    $source = $direct['headroom_projection_plan'] ?? null;
    if ($source === null) {
        return ['valid' => true, 'reason' => null, 'present' => false, 'revision' => null, 'slots_by_bounds' => [], 'groups_by_id' => []];
    }
    if (!is_array($source)) return $invalid('DIRECT_MARKETING_HEADROOM_PROJECTION_PLAN_INVALID');

    $topKeys = array_keys($source);
    sort($topKeys, SORT_STRING);
    $expectedTopKeys = ['commands_allowed', 'complete', 'effective_duration_s', 'effective_end_ts', 'effective_start_ts', 'energy_basis', 'executable', 'generated_at_ts', 'group_count', 'groups', 'hardware_effect', 'invalid_slot_count', 'projected_action', 'projected_mode', 'projected_source_action', 'projection_only', 'revision', 'schema', 'slot_count', 'slot_duration_s', 'slots', 'status'];
    if ($topKeys !== $expectedTopKeys) return $invalid('DIRECT_MARKETING_HEADROOM_PROJECTION_PLAN_SCHEMA_INVALID');

    $normalizeMode = static function($value) {
        $mode = strtolower(str_replace(['-', ' '], '_', trim((string)$value)));
        return in_array($mode, ['eco+', 'ecoplus'], true) ? 'eco_plus' : $mode;
    };
    $finite = static function($value) {
        return (is_int($value) || is_float($value)) && is_finite((float)$value);
    };
    $revision = (string)($source['revision'] ?? '');
    $rootMode = $normalizeMode($source['projected_mode'] ?? null);
    $directMode = $normalizeMode($direct['mode'] ?? null);
    $generatedAt = $source['generated_at_ts'] ?? null;
    $material = $source;
    unset($material['revision']);
    $encoded = liveTrajectoryCanonicalJson($material);
    $calculated = is_string($encoded) ? 'sha256:' . hash('sha256', $encoded) : '';
    if (($source['schema'] ?? '') !== 'direct_marketing_headroom_projection_plan_v1'
        || ($source['energy_basis'] ?? '') !== 'stored_battery_energy_delta_before_discharge_loss_v1'
        || !is_int($generatedAt) || $generatedAt <= 0
        || $generatedAt !== (int)($direct['created_ts'] ?? 0)
        || !$finite($source['effective_duration_s'] ?? null) || (float)$source['effective_duration_s'] < 0.0
        || ($source['projection_only'] ?? null) !== true
        || ($source['executable'] ?? null) !== false
        || ($source['commands_allowed'] ?? null) !== false
        || ($source['hardware_effect'] ?? null) !== false
        || ($source['complete'] ?? null) !== true
        || ($source['status'] ?? '') !== 'complete'
        || ($source['invalid_slot_count'] ?? null) !== 0
        || ($source['slot_duration_s'] ?? null) !== 900
        || ($source['projected_action'] ?? '') !== 'HEADROOM_EXPORT'
        || ($source['projected_source_action'] ?? '') !== 'eco_plus_negative_headroom_hold'
        || $rootMode === '' || $rootMode !== $directMode
        || !is_array($source['slots'] ?? null) || !is_array($source['groups'] ?? null)
        || !is_int($source['slot_count'] ?? null) || !is_int($source['group_count'] ?? null)
        || (int)$source['slot_count'] !== count($source['slots'])
        || (int)$source['group_count'] !== count($source['groups'])
        || (((int)$source['slot_count'] > 0 || (int)$source['group_count'] > 0) && $rootMode !== 'eco_plus')
        || preg_match('/^sha256:[0-9a-f]{64}$/', $revision) !== 1
        || $calculated === '' || !hash_equals($revision, $calculated)) {
        return $invalid('DIRECT_MARKETING_HEADROOM_PROJECTION_PLAN_INVALID');
    }

    $groupsById = [];
    $previousGroupId = null;
    foreach ($source['groups'] as $group) {
        if (!is_array($group)) return $invalid('DIRECT_MARKETING_HEADROOM_PROJECTION_GROUP_INVALID');
        $groupKeys = array_keys($group);
        sort($groupKeys, SORT_STRING);
        if ($groupKeys !== ['effective_duration_s', 'effective_end_ts', 'effective_start_ts', 'energy_basis', 'forecast_absorption_wh', 'headroom_deficit_wh', 'headroom_export_budget_id', 'headroom_export_budget_wh', 'headroom_free_before_wh', 'headroom_required_wh', 'next_charge_end_ts', 'next_charge_start_ts', 'projected_energy_wh', 'projection_horizon_contract', 'protected_reserve_wh', 'reserve_floor_soc_pct', 'segment_id', 'sellable_wh', 'slot_ids', 'target_soc_pct', 'window_end_ts', 'window_id', 'window_start_ts']) return $invalid('DIRECT_MARKETING_HEADROOM_PROJECTION_GROUP_INVALID');
        $budgetId = (string)($group['headroom_export_budget_id'] ?? '');
        $slotIds = $group['slot_ids'] ?? null;
        if (($group['energy_basis'] ?? '') !== 'stored_battery_energy_delta_before_discharge_loss_v1'
            || preg_match('/^headroom-budget:[0-9a-f]{64}$/', $budgetId) !== 1
            || isset($groupsById[$budgetId])
            || ($previousGroupId !== null && strcmp($previousGroupId, $budgetId) >= 0)
            || !is_array($slotIds) || count($slotIds) === 0 || count($slotIds) !== count(array_unique($slotIds))
            || !$finite($group['headroom_export_budget_wh'] ?? null) || (float)$group['headroom_export_budget_wh'] <= 0.0
            || !$finite($group['projected_energy_wh'] ?? null) || (float)$group['projected_energy_wh'] <= 0.0
            || (float)$group['projected_energy_wh'] > (float)$group['headroom_export_budget_wh'] + 1.0
            || ($group['projection_horizon_contract'] ?? '') !== 'ordered_unique_slots_non_contiguous_allowed_v1'
            || preg_match('/^headroom-window:[0-9a-f]{64}$/', (string)($group['window_id'] ?? '')) !== 1
            || preg_match('/^headroom-segment:[0-9a-f]{64}$/', (string)($group['segment_id'] ?? '')) !== 1
            || !is_int($group['window_start_ts'] ?? null) || !is_int($group['window_end_ts'] ?? null)
            || (int)$group['window_end_ts'] <= (int)$group['window_start_ts']
            || !is_int($group['effective_start_ts'] ?? null) || !is_int($group['effective_end_ts'] ?? null)
            || (int)$group['effective_start_ts'] < (int)$group['window_start_ts']
            || (int)$group['effective_end_ts'] > (int)$group['window_end_ts']
            || (int)$group['effective_end_ts'] <= (int)$group['effective_start_ts']
            || !$finite($group['effective_duration_s'] ?? null) || (float)$group['effective_duration_s'] <= 0.0
            || (float)$group['effective_duration_s'] > ((int)$group['effective_end_ts'] - (int)$group['effective_start_ts']) / 1000.0 + 0.000001
            || !$finite($group['reserve_floor_soc_pct'] ?? null) || (float)$group['reserve_floor_soc_pct'] < 0.0 || (float)$group['reserve_floor_soc_pct'] > 100.0
            || !$finite($group['target_soc_pct'] ?? null) || (float)$group['target_soc_pct'] < (float)$group['reserve_floor_soc_pct'] || (float)$group['target_soc_pct'] > 100.0
            || !$finite($group['protected_reserve_wh'] ?? null) || (float)$group['protected_reserve_wh'] < 0.0
            || !$finite($group['sellable_wh'] ?? null) || (float)$group['sellable_wh'] < 0.0
            || !$finite($group['headroom_deficit_wh'] ?? null) || (float)$group['headroom_deficit_wh'] < 0.0
            || !$finite($group['headroom_required_wh'] ?? null) || (float)$group['headroom_required_wh'] < 0.0
            || !$finite($group['headroom_free_before_wh'] ?? null) || (float)$group['headroom_free_before_wh'] < 0.0
            || !$finite($group['forecast_absorption_wh'] ?? null) || (float)$group['forecast_absorption_wh'] < 0.0
            || !is_int($group['next_charge_start_ts'] ?? null) || !is_int($group['next_charge_end_ts'] ?? null)
            || (int)$group['next_charge_start_ts'] < (int)$group['window_end_ts']
            || (int)$group['next_charge_end_ts'] <= (int)$group['next_charge_start_ts']) {
            return $invalid('DIRECT_MARKETING_HEADROOM_PROJECTION_GROUP_INVALID');
        }
        foreach ($slotIds as $slotId) if (!is_string($slotId) || preg_match('/^headroom-slot:[0-9a-f]{64}$/', $slotId) !== 1) return $invalid('DIRECT_MARKETING_HEADROOM_PROJECTION_GROUP_INVALID');
        $groupsById[$budgetId] = $group;
        $previousGroupId = $budgetId;
    }

    $slotsByBounds = [];
    $groupSlotIds = [];
    $groupEnergyWh = [];
    $previousEnd = null;
    foreach ($source['slots'] as $slot) {
        if (!is_array($slot)) return $invalid('DIRECT_MARKETING_HEADROOM_PROJECTION_SLOT_INVALID');
        $slotKeys = array_keys($slot);
        sort($slotKeys, SORT_STRING);
        if ($slotKeys !== ['commands_allowed', 'duration_s', 'effective_duration_s', 'effective_start_ts', 'effective_window_duration_s', 'effective_window_end_ts', 'effective_window_start_ts', 'end_ts', 'energy_basis', 'executable', 'forecast_absorption_wh', 'hardware_effect', 'headroom_deficit_wh', 'headroom_export_budget_id', 'headroom_export_budget_wh', 'headroom_export_slot_energy_wh', 'headroom_export_slot_id', 'headroom_free_before_wh', 'headroom_required_wh', 'next_charge_end_ts', 'next_charge_start_ts', 'projected_action', 'projected_mode', 'projected_power_w', 'projected_source_action', 'projection_horizon_contract', 'projection_id', 'projection_only', 'protected_reserve_wh', 'reserve_floor_soc_pct', 'segment_id', 'sellable_wh', 'start_ts', 'target_soc_pct', 'window_end_ts', 'window_id', 'window_start_ts']) return $invalid('DIRECT_MARKETING_HEADROOM_PROJECTION_SLOT_INVALID');
        $slotId = (string)($slot['headroom_export_slot_id'] ?? '');
        $budgetId = (string)($slot['headroom_export_budget_id'] ?? '');
        $start = $slot['start_ts'] ?? null;
        $end = $slot['end_ts'] ?? null;
        $powerW = $slot['projected_power_w'] ?? null;
        $energyWh = $slot['headroom_export_slot_energy_wh'] ?? null;
        $effectiveStart = $slot['effective_start_ts'] ?? null;
        $effectiveDurationS = $slot['effective_duration_s'] ?? null;
        $boundsKey = is_int($start) && is_int($end) ? $start . ':' . $end : '';
        if (($slot['energy_basis'] ?? '') !== 'stored_battery_energy_delta_before_discharge_loss_v1'
            || preg_match('/^headroom-slot:[0-9a-f]{64}$/', $slotId) !== 1
            || ($slot['projection_id'] ?? null) !== $slotId
            || !isset($groupsById[$budgetId]) || $boundsKey === '' || isset($slotsByBounds[$boundsKey])
            || !is_int($start) || !is_int($end) || $end - $start !== 900000
            || !is_int($effectiveStart) || $effectiveStart !== max($start, $generatedAt) || $effectiveStart >= $end
            || !$finite($effectiveDurationS) || (float)$effectiveDurationS <= 0.0 || (float)$effectiveDurationS > 900.0
            || abs((float)$effectiveDurationS - (($end - $effectiveStart) / 1000.0)) > 0.000001
            || ($previousEnd !== null && $start < $previousEnd)
            || ($slot['duration_s'] ?? null) !== 900
            || ($slot['projection_only'] ?? null) !== true
            || ($slot['executable'] ?? null) !== false
            || ($slot['commands_allowed'] ?? null) !== false
            || ($slot['hardware_effect'] ?? null) !== false
            || ($slot['projected_action'] ?? '') !== 'HEADROOM_EXPORT'
            || ($slot['projected_source_action'] ?? '') !== 'eco_plus_negative_headroom_hold'
            || $normalizeMode($slot['projected_mode'] ?? null) !== $rootMode
            || ($slot['projection_horizon_contract'] ?? '') !== 'ordered_unique_slots_non_contiguous_allowed_v1'
            || preg_match('/^headroom-window:[0-9a-f]{64}$/', (string)($slot['window_id'] ?? '')) !== 1
            || preg_match('/^headroom-segment:[0-9a-f]{64}$/', (string)($slot['segment_id'] ?? '')) !== 1
            || !is_int($slot['window_start_ts'] ?? null) || !is_int($slot['window_end_ts'] ?? null)
            || (int)$slot['window_end_ts'] <= (int)$slot['window_start_ts']
            || $start < (int)$slot['window_start_ts'] || $end > (int)$slot['window_end_ts']
            || !is_int($powerW) || $powerW <= 0
            || !$finite($energyWh) || abs((float)$energyWh - ((float)$powerW * (float)$effectiveDurationS / 3600.0)) > 0.000501
            || !$finite($slot['headroom_export_budget_wh'] ?? null) || (float)$slot['headroom_export_budget_wh'] + 0.001 < (float)$energyWh
            || !$finite($slot['reserve_floor_soc_pct'] ?? null) || (float)$slot['reserve_floor_soc_pct'] < 0.0 || (float)$slot['reserve_floor_soc_pct'] > 100.0
            || !$finite($slot['target_soc_pct'] ?? null) || (float)$slot['target_soc_pct'] < (float)$slot['reserve_floor_soc_pct'] || (float)$slot['target_soc_pct'] > 100.0
            || !$finite($slot['protected_reserve_wh'] ?? null) || (float)$slot['protected_reserve_wh'] < 0.0
            || !$finite($slot['sellable_wh'] ?? null) || (float)$slot['sellable_wh'] < 0.0
            || !$finite($slot['headroom_deficit_wh'] ?? null) || (float)$slot['headroom_deficit_wh'] < 0.0
            || !$finite($slot['headroom_required_wh'] ?? null) || (float)$slot['headroom_required_wh'] < 0.0
            || !$finite($slot['headroom_free_before_wh'] ?? null) || (float)$slot['headroom_free_before_wh'] < 0.0
            || !$finite($slot['forecast_absorption_wh'] ?? null) || (float)$slot['forecast_absorption_wh'] < 0.0
            || !is_int($slot['next_charge_start_ts'] ?? null) || !is_int($slot['next_charge_end_ts'] ?? null)
            || (int)$slot['next_charge_start_ts'] < $end || (int)$slot['next_charge_end_ts'] <= (int)$slot['next_charge_start_ts']) {
            return $invalid('DIRECT_MARKETING_HEADROOM_PROJECTION_SLOT_INVALID');
        }
        $group = $groupsById[$budgetId];
        $identity = [
            'schema' => 'direct_marketing_headroom_projection_plan_v1',
            'energy_basis' => 'stored_battery_energy_delta_before_discharge_loss_v1',
            'headroom_export_budget_id' => $budgetId,
            'window_id' => $group['window_id'],
            'segment_id' => $group['segment_id'],
            'start_ts' => $start,
            'end_ts' => $end,
            'effective_start_ts' => $effectiveStart,
            'effective_duration_s' => $effectiveDurationS,
            'effective_window_start_ts' => $group['effective_start_ts'],
            'effective_window_end_ts' => $group['effective_end_ts'],
            'effective_window_duration_s' => $group['effective_duration_s'],
            'projected_action' => 'HEADROOM_EXPORT',
            'projected_source_action' => 'eco_plus_negative_headroom_hold',
            'projected_mode' => $rootMode,
            'projected_power_w' => $powerW,
            'headroom_export_slot_energy_wh' => $energyWh,
        ];
        $identityEncoded = liveTrajectoryCanonicalJson($identity);
        $identityRevision = is_string($identityEncoded) ? hash('sha256', $identityEncoded) : '';
        if ($identityRevision === '' || !hash_equals('headroom-slot:' . $identityRevision, $slotId)) return $invalid('DIRECT_MARKETING_HEADROOM_PROJECTION_SLOT_ID_INVALID');
        $windowIdentityEncoded = liveTrajectoryCanonicalJson([
            'schema' => 'direct_marketing_headroom_projection_plan_v1',
            'energy_basis' => 'stored_battery_energy_delta_before_discharge_loss_v1',
            'headroom_export_budget_id' => $budgetId,
            'window_start_ts' => $group['window_start_ts'],
            'window_end_ts' => $group['window_end_ts'],
            'effective_start_ts' => $group['effective_start_ts'],
            'effective_end_ts' => $group['effective_end_ts'],
            'effective_duration_s' => $group['effective_duration_s'],
        ]);
        $segmentIdentityEncoded = liveTrajectoryCanonicalJson([
            'schema' => 'direct_marketing_headroom_projection_plan_v1',
            'energy_basis' => 'stored_battery_energy_delta_before_discharge_loss_v1',
            'headroom_export_budget_id' => $budgetId,
            'reserve_floor_soc_pct' => $group['reserve_floor_soc_pct'],
            'target_soc_pct' => $group['target_soc_pct'],
            'effective_start_ts' => $group['effective_start_ts'],
            'effective_end_ts' => $group['effective_end_ts'],
            'effective_duration_s' => $group['effective_duration_s'],
        ]);
        $expectedWindowId = is_string($windowIdentityEncoded) ? 'headroom-window:' . hash('sha256', $windowIdentityEncoded) : '';
        $expectedSegmentId = is_string($segmentIdentityEncoded) ? 'headroom-segment:' . hash('sha256', $segmentIdentityEncoded) : '';
        if ($expectedWindowId === '' || !hash_equals($expectedWindowId, (string)$slot['window_id'])
            || $expectedSegmentId === '' || !hash_equals($expectedSegmentId, (string)$slot['segment_id'])) {
            return $invalid('DIRECT_MARKETING_HEADROOM_PROJECTION_IDENTITY_INVALID');
        }
        if ($slot['effective_window_start_ts'] !== $group['effective_start_ts']
            || $slot['effective_window_end_ts'] !== $group['effective_end_ts']
            || $slot['effective_window_duration_s'] !== $group['effective_duration_s']) {
            return $invalid('DIRECT_MARKETING_HEADROOM_PROJECTION_EFFECTIVE_WINDOW_BINDING_INVALID');
        }
        foreach (['energy_basis', 'headroom_export_budget_wh', 'reserve_floor_soc_pct', 'target_soc_pct', 'protected_reserve_wh', 'sellable_wh', 'headroom_deficit_wh', 'headroom_required_wh', 'headroom_free_before_wh', 'forecast_absorption_wh', 'next_charge_start_ts', 'next_charge_end_ts', 'window_id', 'segment_id', 'window_start_ts', 'window_end_ts', 'projection_horizon_contract'] as $key) {
            if ($slot[$key] !== $group[$key]) return $invalid('DIRECT_MARKETING_HEADROOM_PROJECTION_GROUP_BINDING_INVALID');
        }
        $slotsByBounds[$boundsKey] = $slot;
        $groupSlotIds[$budgetId][] = $slotId;
        $groupEnergyWh[$budgetId] = ($groupEnergyWh[$budgetId] ?? 0.0) + (float)$energyWh;
        $previousEnd = $end;
    }
    foreach ($groupsById as $budgetId => $group) {
        if (($groupSlotIds[$budgetId] ?? []) !== $group['slot_ids']
            || abs(($groupEnergyWh[$budgetId] ?? 0.0) - (float)$group['projected_energy_wh']) > 0.001) {
            return $invalid('DIRECT_MARKETING_HEADROOM_PROJECTION_GROUP_BINDING_INVALID');
        }
        $boundSlots = array_values(array_filter($source['slots'], static function($slot) use ($budgetId) {
            return is_array($slot) && ($slot['headroom_export_budget_id'] ?? null) === $budgetId;
        }));
        $boundStarts = array_map(static function($slot) { return (int)$slot['start_ts']; }, $boundSlots);
        $boundEnds = array_map(static function($slot) { return (int)$slot['end_ts']; }, $boundSlots);
        if (count($boundSlots) === 0
            || min($boundStarts) !== (int)$group['window_start_ts']
            || max($boundEnds) !== (int)$group['window_end_ts']
            || min(array_map(static function($slot) { return (int)$slot['effective_start_ts']; }, $boundSlots)) !== (int)$group['effective_start_ts']
            || max($boundEnds) !== (int)$group['effective_end_ts']
            || abs(array_sum(array_map(static function($slot) { return (float)$slot['effective_duration_s']; }, $boundSlots)) - (float)$group['effective_duration_s']) > 0.000001) {
            return $invalid('DIRECT_MARKETING_HEADROOM_PROJECTION_WINDOW_BINDING_INVALID');
        }
    }
    if (count($source['slots']) === 0) {
        if (($source['effective_start_ts'] ?? null) !== null
            || ($source['effective_end_ts'] ?? null) !== null
            || (float)$source['effective_duration_s'] !== 0.0) {
            return $invalid('DIRECT_MARKETING_HEADROOM_PROJECTION_EFFECTIVE_HORIZON_INVALID');
        }
    } else {
        $effectiveStarts = array_map(static function($slot) { return (int)$slot['effective_start_ts']; }, $source['slots']);
        $effectiveEnds = array_map(static function($slot) { return (int)$slot['end_ts']; }, $source['slots']);
        $effectiveDuration = array_sum(array_map(static function($slot) { return (float)$slot['effective_duration_s']; }, $source['slots']));
        if (($source['effective_start_ts'] ?? null) !== min($effectiveStarts)
            || ($source['effective_end_ts'] ?? null) !== max($effectiveEnds)
            || abs((float)$source['effective_duration_s'] - $effectiveDuration) > 0.000001) {
            return $invalid('DIRECT_MARKETING_HEADROOM_PROJECTION_EFFECTIVE_HORIZON_INVALID');
        }
    }
    return ['valid' => true, 'reason' => null, 'present' => true, 'revision' => $revision, 'slots_by_bounds' => $slotsByBounds, 'groups_by_id' => $groupsById];
}

function liveHeadroomEnergyBindingValid($binding, $sidecarSlot, $projectedW, $batteryW) {
    if (!is_array($binding) || !is_array($sidecarSlot)) return false;
    $keys = array_keys($binding);
    sort($keys, SORT_STRING);
    $expectedKeys = ['applied_ac_discharge_w', 'applied_stored_delta_wh', 'axis_duration_s', 'bounded', 'bounding_status', 'desired_ac_discharge_w', 'discharge_efficiency', 'effective_duration_s', 'effective_start_ts', 'energy_basis', 'hardware_discharge_limit_w', 'limiting_factors', 'requested_stored_delta_wh', 'reserve_ac_discharge_limit_w', 'reserve_available_stored_wh', 'schema', 'slot_energy_ac_discharge_limit_w', 'stored_delta_rate_w'];
    if ($keys !== $expectedKeys) return false;
    $numericKeys = ['effective_duration_s', 'stored_delta_rate_w', 'requested_stored_delta_wh', 'discharge_efficiency', 'desired_ac_discharge_w', 'hardware_discharge_limit_w', 'reserve_available_stored_wh', 'slot_energy_ac_discharge_limit_w', 'reserve_ac_discharge_limit_w', 'applied_ac_discharge_w', 'applied_stored_delta_wh'];
    foreach ($numericKeys as $key) if ((!is_int($binding[$key] ?? null) && !is_float($binding[$key] ?? null)) || !is_finite((float)$binding[$key]) || (float)$binding[$key] < 0.0) return false;
    if ((!is_int($projectedW) && !is_float($projectedW)) || !is_finite((float)$projectedW) || (float)$projectedW < 0.0
        || (!is_int($batteryW) && !is_float($batteryW)) || !is_finite((float)$batteryW) || (float)$batteryW > 0.0
        || (!is_int($sidecarSlot['projected_power_w'] ?? null) && !is_float($sidecarSlot['projected_power_w'] ?? null))
        || (!is_int($sidecarSlot['headroom_export_slot_energy_wh'] ?? null) && !is_float($sidecarSlot['headroom_export_slot_energy_wh'] ?? null))
        || (!is_int($sidecarSlot['effective_duration_s'] ?? null) && !is_float($sidecarSlot['effective_duration_s'] ?? null))) return false;
    $appliedAcW = (float)$binding['applied_ac_discharge_w'];
    $desiredAcW = (float)$binding['desired_ac_discharge_w'];
    $requestedStoredWh = (float)$binding['requested_stored_delta_wh'];
    $appliedStoredWh = (float)$binding['applied_stored_delta_wh'];
    $status = (string)($binding['bounding_status'] ?? '');
    $bounded = $binding['bounded'] ?? null;
    $statusValid = ($status === 'UNBOUNDED' && $bounded === false && abs($appliedAcW - $desiredAcW) <= 0.001)
        || ($status === 'BOUNDED' && $bounded === true && $appliedAcW > 0.001 && $appliedAcW + 0.001 < $desiredAcW)
        || ($status === 'ZERO_BOUNDED' && $bounded === true && $appliedAcW <= 0.001 && $desiredAcW > 0.001);
    $allowedFactors = ['desired_ac_discharge_w', 'hardware_discharge_limit_w', 'slot_energy_ac_discharge_limit_w', 'reserve_ac_discharge_limit_w'];
    $factors = $binding['limiting_factors'] ?? null;
    if (!is_array($factors) || count($factors) === 0 || count($factors) !== count(array_unique($factors))) return false;
    $previousFactorIndex = -1;
    foreach ($factors as $factor) {
        $factorIndex = array_search($factor, $allowedFactors, true);
        if ($factorIndex === false || $factorIndex <= $previousFactorIndex) return false;
        $previousFactorIndex = $factorIndex;
    }
    return ($binding['schema'] ?? null) === 'direct_marketing_headroom_energy_binding_v1'
        && ($binding['energy_basis'] ?? null) === 'stored_battery_energy_delta_before_discharge_loss_v1'
        && ($binding['axis_duration_s'] ?? null) === 900
        && ($binding['effective_start_ts'] ?? null) === ($sidecarSlot['effective_start_ts'] ?? null)
        && abs((float)$binding['effective_duration_s'] - (float)$sidecarSlot['effective_duration_s']) <= 0.000001
        && abs((float)$binding['stored_delta_rate_w'] - (float)$sidecarSlot['projected_power_w']) <= 0.001
        && abs($requestedStoredWh - (float)$sidecarSlot['headroom_export_slot_energy_wh']) <= 0.001
        && (float)$binding['discharge_efficiency'] > 0.0 && (float)$binding['discharge_efficiency'] <= 1.0
        && $appliedStoredWh <= $requestedStoredWh + 0.001
        && $appliedAcW <= (float)$binding['hardware_discharge_limit_w'] + 0.001
        && $appliedAcW <= (float)$binding['slot_energy_ac_discharge_limit_w'] + 0.001
        && $appliedAcW <= (float)$binding['reserve_ac_discharge_limit_w'] + 0.001
        && abs($appliedAcW - (float)$projectedW) <= 0.001
        && abs($appliedAcW - abs((float)$batteryW)) <= 0.001
        && $statusValid;
}

function liveTrajectoryValidationReason($plan, $source, $canonicalPlan) {
    $planId = is_array($plan) ? (string)($plan['plan_id'] ?? '') : '';
    if (!$canonicalPlan || preg_match('/^sha256:[0-9a-f]{64}$/', $planId) !== 1) {
        return 'DIRECT_MARKETING_CANONICAL_PLAN_INVALID';
    }
    $headroomEvidence = liveHeadroomProjectionEvidence($plan);
    if (($headroomEvidence['present'] ?? false) === true && ($headroomEvidence['valid'] ?? false) !== true) {
        return (string)($headroomEvidence['reason'] ?? 'DIRECT_MARKETING_HEADROOM_PROJECTION_PLAN_INVALID');
    }
    if (!is_array($source)) return 'DIRECT_MARKETING_TRAJECTORY_MISSING';
    if (($source['schema_version'] ?? '') !== 'direct_marketing_trajectory_v1') return 'DIRECT_MARKETING_TRAJECTORY_SCHEMA_INVALID';
    if (($source['active'] ?? null) !== true || ($source['complete'] ?? null) !== true) {
        return $source['reason_code'] ?? $source['status'] ?? 'DIRECT_MARKETING_TRAJECTORY_INCOMPLETE';
    }
    if (!in_array((string)($source['status'] ?? ''), ['COMPLETE', 'COMPLETE_BOUNDED'], true)) {
        return 'DIRECT_MARKETING_TRAJECTORY_STATUS_INVALID';
    }
    if (($source['plan_id'] ?? null) !== $planId) return 'DIRECT_MARKETING_TRAJECTORY_PLAN_MISMATCH';
    if (!is_array($source['meta'] ?? null) || !is_array($source['slots'] ?? null) || count($source['slots']) === 0) {
        return 'DIRECT_MARKETING_TRAJECTORY_STRUCTURE_INCOMPLETE';
    }
    if (liveTrajectoryCanonicalJson($source['input_revisions'] ?? null)
        !== liveTrajectoryCanonicalJson($plan['input_revisions'] ?? null)) {
        return 'DIRECT_MARKETING_TRAJECTORY_INPUT_REVISION_MISMATCH';
    }
    $revision = (string)($source['trajectory_revision'] ?? '');
    $material = $source;
    unset($material['trajectory_revision']);
    $encoded = liveTrajectoryCanonicalJson($material);
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
    $previousSlotId = null;
    $headroomProjectionIds = [];
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
        $batteryW = (float)$slot['battery_w'];
        $action = strtoupper((string)($slot['action'] ?? ''));
        $projection = is_array($planSlot['projection'] ?? null) ? $planSlot['projection'] : [];
        $provenance = is_array($slot['provenance'] ?? null) ? $slot['provenance'] : [];
        $boundsKey = $start . ':' . $end;
        $headroomSource = $headroomEvidence['slots_by_bounds'][$boundsKey] ?? null;
        $socProjectionContract = (string)($provenance['soc_projection_contract'] ?? '');
        $planSoc = is_array($planSlot['soc_pct'] ?? null) ? $planSlot['soc_pct'] : [];
        $directCreatedTs = (int)($plan['direct_marketing']['created_ts'] ?? 0);
        $standardPassthrough = $socProjectionContract === 'canonical_standard_soc_passthrough_v1';
        $standardPassthroughValid = $standardPassthrough
            && $action === 'PASSIVE_NORMAL'
            && $start <= $directCreatedTs && $directCreatedTs < $end
            && is_numeric($plan['current_soc'] ?? null)
            && is_numeric($planSoc['end'] ?? null)
            && is_numeric($projection['battery_w'] ?? null)
            && is_numeric($projection['grid_w'] ?? null)
            && abs((float)$slot['soc_start_pct'] - (float)$plan['current_soc']) <= 0.0015
            && abs((float)$slot['soc_end_pct'] - (float)$planSoc['end']) <= 0.0015
            && abs($batteryW - (float)$projection['battery_w']) <= 0.001
            && abs((float)$slot['grid_w'] - (float)$projection['grid_w']) <= 0.001;
        $standardTransition = $socProjectionContract === 'canonical_standard_transition_rebased_v1';
        $transitionAnchorTs = $provenance['integration_anchor_ts_ms'] ?? null;
        $transitionDurationS = $provenance['integration_duration_s'] ?? null;
        $currentTransition = $start === (int)($plan['valid_from_ts_ms'] ?? 0);
        $expectedTransitionAnchorTs = $currentTransition
            ? ($plan['generated_at_ts_ms'] ?? null)
            : $start;
        $expectedTransitionDurationS = is_int($expectedTransitionAnchorTs)
            ? ($end - $expectedTransitionAnchorTs) / 1000.0
            : null;
        $standardTransitionDurationValid =
            ($provenance['integration_duration_contract'] ?? null)
                === 'canonical_standard_transition_duration_v1'
            && is_int($transitionAnchorTs)
            && is_int($expectedTransitionAnchorTs)
            && $transitionAnchorTs === $expectedTransitionAnchorTs
            && $start <= $transitionAnchorTs && $transitionAnchorTs < $end
            && (is_int($transitionDurationS) || is_float($transitionDurationS))
            && !is_bool($transitionDurationS)
            && is_finite((float)$transitionDurationS)
            && (float)$transitionDurationS > 0.0
            && (float)$transitionDurationS <= 900.0
            && is_float($expectedTransitionDurationS)
            && abs((float)$transitionDurationS - $expectedTransitionDurationS) <= 0.001;
        $standardTransitionValid = $standardTransition
            && ($provenance['soc_transition_contract'] ?? null) === 'canonical_standard_transition_rebased_v1'
            && $action === 'PASSIVE_NORMAL'
            && ($slot['projection_only'] ?? null) !== true
            && ($provenance['predecessor_slot_id'] ?? null) === $previousSlotId
            && is_numeric($planSoc['start'] ?? null)
            && is_numeric($projection['battery_w'] ?? null)
            && is_numeric($provenance['canonical_standard_start_soc_pct'] ?? null)
            && is_numeric($provenance['rebased_start_soc_pct'] ?? null)
            && is_numeric($provenance['standard_requested_battery_w'] ?? null)
            && $standardTransitionDurationValid
            && abs((float)$provenance['canonical_standard_start_soc_pct'] - (float)$planSoc['start']) <= 0.0015
            && abs((float)$provenance['rebased_start_soc_pct'] - (float)$slot['soc_start_pct']) <= 0.0015
            && abs((float)$provenance['standard_requested_battery_w'] - (float)$projection['battery_w']) <= 0.001;
        $transitionFieldPresent = false;
        foreach (['soc_transition_contract', 'predecessor_slot_id', 'canonical_standard_start_soc_pct', 'rebased_start_soc_pct', 'standard_requested_battery_w', 'integration_duration_contract', 'integration_anchor_ts_ms', 'integration_duration_s'] as $key) {
            if (array_key_exists($key, $provenance)) $transitionFieldPresent = true;
        }
        if (($standardPassthrough && !$standardPassthroughValid)
            || ($standardTransition && !$standardTransitionValid)
            || (!$standardTransition && $transitionFieldPresent)
            || (!$standardPassthrough && !$standardTransition
                && $socProjectionContract !== 'direct_marketing_energy_integrator_v1')) {
            return 'DIRECT_MARKETING_TRAJECTORY_SOC_PROJECTION_CONTRACT_INVALID';
        }
        $selection = is_array($slot['selection'] ?? null) ? $slot['selection'] : [];
        $delegation = is_array($slot['delegation'] ?? null) ? $slot['delegation'] : null;
        $standardProjectionBinding = is_array($slot['standard_projection_binding'] ?? null)
            ? $slot['standard_projection_binding']
            : null;
        $standardProjectionBindingKeys = is_array($standardProjectionBinding)
            ? array_keys($standardProjectionBinding)
            : [];
        sort($standardProjectionBindingKeys, SORT_STRING);
        $standardProjectionBindingValid = is_array($standardProjectionBinding)
            && $standardProjectionBindingKeys === [
                'commands_allowed', 'executable', 'hardware_effect',
                'projection_only', 'schema', 'source_revision', 'source_schema'
            ]
            && ($standardProjectionBinding['schema'] ?? null) === 'canonical_standard_projection_binding_v1'
            && ($standardProjectionBinding['projection_only'] ?? null) === true
            && ($standardProjectionBinding['executable'] ?? null) === false
            && ($standardProjectionBinding['commands_allowed'] ?? null) === false
            && ($standardProjectionBinding['hardware_effect'] ?? null) === false
            && ($standardProjectionBinding['source_schema'] ?? null) === 'direct_marketing_headroom_projection_plan_v1'
            && preg_match('/^sha256:[0-9a-f]{64}$/', (string)($standardProjectionBinding['source_revision'] ?? '')) === 1
            && ($headroomEvidence['present'] ?? false) === true
            && ($headroomEvidence['valid'] ?? false) === true
            && hash_equals(
                (string)($headroomEvidence['revision'] ?? ''),
                (string)($standardProjectionBinding['source_revision'] ?? '')
            );
        $headroomProjection = is_array($slot['headroom_projection'] ?? null) ? $slot['headroom_projection'] : null;
        $projectionOnlyMarker = $action === 'HEADROOM_EXPORT'
            || array_key_exists('action_role', $slot)
            || array_key_exists('projection_only', $slot)
            || array_key_exists('hardware_effect', $slot)
            || array_key_exists('headroom_projection', $slot)
            || array_key_exists('projected_action', $selection)
            || array_key_exists('projected_w', $selection)
            || array_key_exists('projection_id', $selection);
        $projectionRole = false;
        if ($projectionOnlyMarker) {
            $selectionKeys = array_keys($selection);
            sort($selectionKeys, SORT_STRING);
            $expectedSelectionKeys = ['commands_allowed', 'executable', 'projected_action', 'projected_w', 'projection_id', 'selected'];
            $expectedBinding = is_array($headroomSource) ? [
                'schema' => 'direct_marketing_headroom_projection_binding_v1',
                'projection_only' => true,
                'projection_plan_revision' => $headroomEvidence['revision'],
                'slot' => $headroomSource,
            ] : null;
            $canonicalActiveMarker = false;
            foreach (['direct_marketing_selected', 'direct_marketing_plan_executable', 'direct_marketing_plan_commands_allowed'] as $key) {
                if (($projection[$key] ?? null) === true) $canonicalActiveMarker = true;
            }
            foreach (['direct_marketing_plan_action', 'direct_marketing_plan_source_action', 'direct_marketing_plan_source_mode', 'direct_marketing_plan_action_id', 'direct_marketing_plan_segment_id', 'direct_marketing_window_id', 'direct_marketing_action_horizon_contract', 'direct_marketing_headroom_export_gate', 'direct_marketing_plan_selected_action', 'direct_marketing_plan_executable_action', 'direct_marketing_effective_action'] as $key) {
                if (($projection[$key] ?? null) !== null && ($projection[$key] ?? null) !== '') $canonicalActiveMarker = true;
            }
            foreach (['direct_marketing_planned_w', 'direct_marketing_requested_w'] as $key) {
                if (array_key_exists($key, $projection) && $projection[$key] !== null
                    && (!is_numeric($projection[$key]) || abs((float)$projection[$key]) > 0.001)) {
                    $canonicalActiveMarker = true;
                }
            }
            $roles = is_array($projection['direct_marketing_action_roles'] ?? null) ? $projection['direct_marketing_action_roles'] : [];
            if (($roles['plan_selected_action'] ?? null) !== null
                || ($roles['plan_executable_action'] ?? null) !== null
                || ($roles['effective_action'] ?? null) !== null
                || ($roles['runtime_effect_claim_allowed'] ?? null) === true) {
                $canonicalActiveMarker = true;
            }
            $runtimeHoldPlannedW = $projection['direct_marketing_planned_w'] ?? null;
            $runtimeHoldValid = $canonicalActiveMarker
                && is_numeric($runtimeHoldPlannedW)
                && abs((float)$runtimeHoldPlannedW) <= 0.001
                && ($projection['direct_marketing_headroom_export_gate'] ?? null) === null
                && liveDirectMarketingActionBindingValid(
                    $plan,
                    $projection,
                    'CHARGE_BLOCK_WAIT',
                    0.0,
                    $start,
                    $end,
                    $validFromMs,
                    $horizonEndMs
                );
            $projectedW = $selection['projected_w'] ?? null;
            $projectionId = (string)($selection['projection_id'] ?? '');
            $hardReservePct = $slot['hard_reserve_soc_pct'] ?? null;
            $protectedReserveWh = is_array($headroomSource) ? ($headroomSource['protected_reserve_wh'] ?? null) : null;
            $reserveFloorPct = is_array($headroomSource) ? ($headroomSource['reserve_floor_soc_pct'] ?? null) : null;
            $provenanceKeys = array_keys($provenance);
            sort($provenanceKeys, SORT_STRING);
            $expectedProvenanceKeys = ['action_source', 'balance_source', 'candidate_effect', 'headroom_energy_binding', 'pv_axis_evidence_class', 'shadow_effect', 'soc_projection_contract'];
            $energyBinding = $provenance['headroom_energy_binding'] ?? null;
            $forbiddenSlotAuthority = false;
            foreach (['requested_w', 'plan_action', 'gate', 'selected', 'executable', 'commands_allowed', 'runtime_effect_claim_allowed', 'action_id', 'window_id', 'segment_id', 'source_action', 'source_mode', 'headroom_export_gate'] as $key) {
                if (array_key_exists($key, $slot)) $forbiddenSlotAuthority = true;
            }
            if ($action !== 'HEADROOM_EXPORT'
                || ($slot['action_role'] ?? null) !== 'PROJECTION_ONLY'
                || ($slot['projection_only'] ?? null) !== true
                || ($slot['hardware_effect'] ?? null) !== false
                || $forbiddenSlotAuthority
                || $selectionKeys !== $expectedSelectionKeys
                || ($selection['selected'] ?? null) !== false
                || ($selection['executable'] ?? null) !== false
                || ($selection['commands_allowed'] ?? null) !== false
                || ($selection['projected_action'] ?? null) !== 'HEADROOM_EXPORT'
                || !is_numeric($projectedW) || !is_finite((float)$projectedW) || (float)$projectedW < 0.0
                || !is_array($headroomSource) || $projectionId !== ($headroomSource['projection_id'] ?? null)
                || isset($headroomProjectionIds[$projectionId])
                || !is_array($headroomProjection)
                || liveTrajectoryCanonicalJson($headroomProjection) !== liveTrajectoryCanonicalJson($expectedBinding)
                || liveTrajectoryCanonicalJson($projection['direct_marketing_headroom_projection'] ?? null) !== liveTrajectoryCanonicalJson($expectedBinding)
                || ($canonicalActiveMarker && !$runtimeHoldValid)
                || $delegation !== null || ($slot['passive_binding'] ?? null) !== null
                || ($slot['standard_projection_binding'] ?? null) !== null
                || ($provenance['action_source'] ?? null) !== 'direct_marketing.headroom_projection_plan'
                || ($provenance['candidate_effect'] ?? null) !== false
                || ($provenance['shadow_effect'] ?? null) !== false
                || $provenanceKeys !== $expectedProvenanceKeys
                || !liveHeadroomEnergyBindingValid($energyBinding, $headroomSource, $projectedW, $batteryW)
                || !is_numeric($protectedReserveWh) || !is_numeric($reserveFloorPct)
                || !is_numeric($hardReservePct) || (float)$hardReservePct + 0.002 < (float)$reserveFloorPct
                || (float)$slot['soc_end_pct'] + 0.002 < (float)$hardReservePct
                || $batteryW > 0.0 || abs(abs($batteryW) - (float)$projectedW) > 0.001) {
                return 'DIRECT_MARKETING_HEADROOM_PROJECTION_ROLE_INVALID';
            }
            $headroomProjectionIds[$projectionId] = true;
            $projectionRole = true;
        }
        $integrationDurationS = $projectionRole
            ? (float)($headroomSource['effective_duration_s'] ?? 0.0)
            : ($standardTransitionValid
                ? (float)$transitionDurationS
                : $durationMs / 1000.0);
        if (!is_finite($integrationDurationS) || $integrationDurationS <= 0.0
            || $integrationDurationS > $durationMs / 1000.0 + 0.001) {
            return 'DIRECT_MARKETING_TRAJECTORY_SOC_PHYSICS_INVALID';
        }
        if (!$standardPassthroughValid) {
            $expectedSocEnd = (float)$slot['soc_start_pct'];
            if ($batteryW >= 0.0) {
                $expectedSocEnd += $batteryW * ($integrationDurationS / 3600.0)
                    * $chargeEfficiency / $capacityWh * 100.0;
            } else {
                $expectedSocEnd -= abs($batteryW) * ($integrationDurationS / 3600.0)
                    / $dischargeEfficiency / $capacityWh * 100.0;
            }
            $expectedSocEnd = max(0.0, min(100.0, $expectedSocEnd));
            if (!is_finite($expectedSocEnd)
                || abs((float)$slot['soc_end_pct'] - $expectedSocEnd) > 0.002) {
                return 'DIRECT_MARKETING_TRAJECTORY_SOC_PHYSICS_INVALID';
            }
        }
        $selectedAction = in_array($action, ['PV_STORE', 'ECONOMIC_EXPORT', 'CHARGE_BLOCK_WAIT', 'DV_CURVE_CHARGE'], true);
        $delegatedPvStore = $action === 'PV_STORE'
            && is_array($delegation)
            && $standardProjectionBinding === null
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
        if ($projectionRole) {
            // Reine Prognoserolle: keine aktive Plan-/Runtime-Semantik.
        } elseif ($selectedAction && !$delegatedPvStore) {
            if (($selection['selected'] ?? null) !== true
                || ($selection['executable'] ?? null) !== true
                || ($selection['commands_allowed'] ?? null) !== true
                || $standardProjectionBinding !== null
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
            $passiveMetadataClear = true;
            foreach (['action_id', 'window_id', 'segment_id', 'source_action', 'source_mode', 'pv_store_source_contract'] as $key) {
                if (!array_key_exists($key, $selection) || $selection[$key] !== null) $passiveMetadataClear = false;
            }
            $passiveBindingValid = is_array($passiveBinding)
                && ($passiveBinding['schema'] ?? null) === 'direct_marketing_passive_normal_binding_v1'
                && $standardProjectionBinding === null
                && liveTrajectoryCanonicalJson($passiveBinding)
                    === liveTrajectoryCanonicalJson($projection['direct_marketing_passive_normal_binding_v1'] ?? null);
            $transitionWithoutPassiveBinding = $standardProjectionBindingValid
                && $passiveBinding === null
                && ($standardPassthroughValid || $standardTransitionValid);
            if (($selection['selected'] ?? null) !== false
                || ($selection['executable'] ?? null) !== false
                || ($selection['commands_allowed'] ?? null) !== false
                || !is_numeric($selection['requested_w'] ?? null) || (float)$selection['requested_w'] !== 0.0
                || !$passiveMetadataClear || $delegation !== null
                || (!$passiveBindingValid && !$transitionWithoutPassiveBinding)) {
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
        $previousSlotId = $slot['slot_id'];
    }
    if ($previousEnd !== $horizonEndMs) return 'DIRECT_MARKETING_TRAJECTORY_HORIZON_MISMATCH';
    if (count($headroomProjectionIds) !== count($headroomEvidence['slots_by_bounds'] ?? [])) {
        return 'DIRECT_MARKETING_HEADROOM_PROJECTION_COVERAGE_INVALID';
    }
    return null;
}

function liveDirectMarketingTrajectoryForDisplay($plan, $enabled, $canonicalPlan) {
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
    $reason = liveTrajectoryValidationReason($plan, $source, $canonicalPlan);
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

function liveDirectMarketingShouldHideClassicalCurves(
    $activePlan,
    $trajectory,
    $effectivePlanBound,
    $targetProjectionAuthorized
) {
    if ($activePlan !== true || !is_array($trajectory)) return false;
    $completeTrajectory = ($trajectory['active'] ?? null) === true
        && ($trajectory['complete'] ?? null) === true
        && in_array((string)($trajectory['status'] ?? ''), ['COMPLETE', 'COMPLETE_BOUNDED'], true);
    return $completeTrajectory
        && ($effectivePlanBound !== true || $targetProjectionAuthorized !== true);
}

function liveDirectMarketingSelectedActionFallbackForDisplay($plan, $enabled, $canonicalPlan, $planRawJson = null, $artifactSnapshot = null) {
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
    if (!$canonicalPlan || preg_match('/^sha256:[0-9a-f]{64}$/', $planId) !== 1 || !is_array($plan['slots'] ?? null)) return $limited('DIRECT_MARKETING_CANONICAL_PLAN_INVALID');
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
    $artifactEncoded = livePlanCanonicalJsonPreservingObjects($artifactMaterial);
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
    $projectionEncoded = livePlanCanonicalJsonPreservingObjects($projectionMaterial);
    $projectionCalculated = is_string($projectionEncoded) ? 'sha256:' . hash('sha256', $projectionEncoded) : '';
    if (preg_match('/^sha256:[0-9a-f]{64}$/', $projectionRevision) !== 1
        || !hash_equals($projectionRevision, $projectionCalculated)
        || ($artifact['projection_revision'] ?? null) !== $projectionRevision
        || ($binding['projection_revision'] ?? null) !== $projectionRevision
        || ($artifact['status'] ?? null) !== ($projection['status'] ?? null)
        || ($artifact['reason_code'] ?? null) !== ($projection['reason_code'] ?? null)) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_REVISION_INVALID');
    $planArtifactRevision = 'sha256:' . hash('sha256', $planRawJson);
    $inputRevisions = is_array($plan['input_revisions'] ?? null) ? $plan['input_revisions'] : null;
    $inputRevisionEncoded = liveTrajectoryCanonicalJson($inputRevisions);
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
    $trajectoryEncoded = liveTrajectoryCanonicalJson($trajectoryMaterial);
    $trajectoryCalculated = is_string($trajectoryEncoded) ? 'sha256:' . hash('sha256', $trajectoryEncoded) : '';
    $trajectoryStatus = (string)($trajectory['status'] ?? '');
    $trajectoryMeta = is_array($trajectory['meta'] ?? null) ? $trajectory['meta'] : null;
    $passivePolicyBindingMetaValid = $trajectoryStatus !== 'PASSIVE_POLICY_BINDING_MISSING'
        || (is_array($trajectoryMeta)
            && ($trajectoryMeta['candidate_effect'] ?? null) === false
            && ($trajectoryMeta['shadow_effect'] ?? null) === false
            && ($trajectoryMeta['runtime_authorization_separate'] ?? null) === true);
    if (!is_array($trajectory) || ($trajectory['schema_version'] ?? '') !== 'direct_marketing_trajectory_v1' || ($trajectory['active'] ?? null) !== true || ($trajectory['complete'] ?? null) !== false || !in_array($trajectoryStatus, ['TRAJECTORY_AXIS_EVIDENCE_LIMIT', 'PASSIVE_POLICY_BINDING_MISSING'], true) || !$passivePolicyBindingMetaValid || ($trajectory['reason_code'] ?? null) !== null || !is_array($trajectory['slots'] ?? null) || count($trajectory['slots']) !== 0 || ($trajectory['plan_id'] ?? null) !== $planId || preg_match('/^sha256:[0-9a-f]{64}$/', $trajectoryRevision) !== 1 || !hash_equals($trajectoryRevision, $trajectoryCalculated) || (int)($trajectory['generated_at_ts_ms'] ?? 0) !== (int)($plan['generated_at_ts_ms'] ?? 0) || (int)($trajectory['valid_from_ts_ms'] ?? 0) !== (int)($plan['valid_from_ts_ms'] ?? 0) || (int)($trajectory['horizon_end_ts_ms'] ?? 0) !== (int)($plan['horizon_end_ts_ms'] ?? 0) || (int)($trajectory['slot_duration_s'] ?? 0) !== (int)($plan['slot_duration_s'] ?? 0) || liveTrajectoryCanonicalJson($trajectory['input_revisions'] ?? null) !== liveTrajectoryCanonicalJson($plan['input_revisions'] ?? null) || ($binding['trajectory_revision'] ?? null) !== $trajectoryRevision) return $limited('DIRECT_MARKETING_ACTION_PROJECTION_TRAJECTORY_NOT_AXIS_LIMITED');
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
        || liveTrajectoryCanonicalJson($projection['input_revisions'] ?? null) !== liveTrajectoryCanonicalJson($inputRevisions)
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
        $encoded = liveTrajectoryCanonicalJson($value);
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
        $expectedSlotId = 'sha256:' . hash('sha256', liveTrajectoryCanonicalJson(['plan_id' => $planId, 'start_ts_ms' => $start, 'end_ts_ms' => $end]));
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
            $positivePowerAction = in_array($action, ['PV_STORE', 'DV_CURVE_CHARGE', 'ECONOMIC_EXPORT'], true);
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

$data['direct_marketing_trajectory'] = liveDirectMarketingTrajectoryForDisplay(
    null,
    $directMarketingConfigured,
    false
);
$data['direct_marketing_selected_action_fallback'] = liveDirectMarketingSelectedActionFallbackForDisplay(null, $directMarketingConfigured, false);
$auxInverterAddress = trim((string)($auxInverterCfg['direct_marketing_aux_inverter_shelly_ip'] ?? ''));
$externalPvTopology = readExternalPvTopologyEvidence();
$data['pv_external_control_available'] = cfgHasAddress($auxInverterAddress);
$data['pv_external_topology_present'] = !empty($externalPvTopology['topology_present']) || $data['pv_external_control_available'];
$data['pv_external_topology_valid'] = !empty($externalPvTopology['valid']) || $data['pv_external_control_available'];
$data['pv_external_topology_source'] = !empty($externalPvTopology['topology_present'])
    ? (string)$externalPvTopology['source']
    : ($data['pv_external_control_available'] ? 'configured_aux_inverter' : 'none');
$data['pv_external_topology_evidence_state'] = !empty($externalPvTopology['topology_present'])
    ? (string)$externalPvTopology['evidence_state']
    : ($data['pv_external_control_available'] ? 'configured' : (string)($externalPvTopology['evidence_state'] ?? 'unknown'));
$data['pv_external_topology_reason'] = !empty($externalPvTopology['topology_present'])
    ? (string)$externalPvTopology['reason']
    : ($data['pv_external_control_available'] ? 'explicit_configuration' : (string)($externalPvTopology['reason'] ?? 'not_confirmed'));
$data['pv_external_capable'] = $data['pv_external_topology_present'];
$haMode = strtolower(trim((string)($confData['config']['ha_mode'] ?? 'off')));
$isShadowMode = ($haMode === 'shadow');
$data['ha_mode'] = $haMode;
$data['shadow_mode'] = $isShadowMode;
$awmwst = isset($confData['config']['awmwst']) ? parseNumericConfigValue($confData['config']['awmwst'], 19.0) : 19.0;
$awnebenkosten = isset($confData['config']['awnebenkosten']) ? parseNumericConfigValue($confData['config']['awnebenkosten'], 0.0) : 0.0;
$speichergroesse = isset($confData['config']['speichergroesse']) ? parseNumericConfigValue($confData['config']['speichergroesse'], 0.0) : 0.0;
$wurzelzaehler = isset($confData['config']['wurzelzaehler']) ? (int)$confData['config']['wurzelzaehler'] : 0;
$wpTypeCfg = getHeatpumpTypeConfig($confData['config'] ?? []);
$wpConfigured = isHeatpumpEnabledConfig($confData['config'] ?? []);
$data['wp_type'] = (string)$wpTypeCfg;

// V4: Ladeplan aus nativem Python-Schedule (Ramdisk)
$wbPlanFile = '/var/www/html/ramdisk/native_wallbox_schedule.json';
$data['wb_plan_hash'] = ($wbPlanFile && file_exists($wbPlanFile)) ? md5_file($wbPlanFile) : '';


$currentPrice = null;

$currentPrice = null;
$minPrice = null;
$maxPrice = null;
$prices = [];
$priceStartHour = (int)date('G');
$priceInterval = 1;
$forecast = [];

$debugFile = rtrim($paths['install_path'], '/') . '/awattardebug.txt';

// --- NEU: RAM-Disk Caching für awattardebug.txt ---
$awattarCacheFile = '/var/www/html/ramdisk/awattar_cache.json';
$debugMtime = file_exists($debugFile) ? filemtime($debugFile) : 0;
$useCache = false;

// Notstromreserve aus der ersten Zeile auslesen
if (file_exists($debugFile)) {
    $f = @fopen($debugFile, 'r');
    if ($f) {
        $firstLine = @fgets($f);
        if ($firstLine && preg_match('/notstrom\s+([0-9.]+)%/', $firstLine, $matches)) {
            $data['notstrom_reserve'] = (float)$matches[1];
        }
        @fclose($f);
    }
}$scheduledPrice = null;

if (file_exists($awattarCacheFile)) {
    $cacheData = @json_decode(file_get_contents($awattarCacheFile), true);
    if ($cacheData && isset($cacheData['mtime']) && $cacheData['mtime'] === $debugMtime) {
        $useCache = true;
        $scheduledPrice = $cacheData['scheduledPrice'] ?? null;
        $minPrice = $cacheData['minPrice']; $maxPrice = $cacheData['maxPrice'];
        $minCt = $cacheData['minCt']; $minSlot = $cacheData['minSlot'];
        $maxCt = $cacheData['maxCt']; $maxSlot = $cacheData['maxSlot'];
        $prices = $cacheData['prices']; $priceStartHour = $cacheData['priceStartHour'];
        $priceInterval = $cacheData['priceInterval'];
        $forecast = isset($cacheData['forecast']) ? $cacheData['forecast'] : [];
    }
}

// --- Statischer Preis-Fallback (V4): Wenn kein Eco-Score und kein dynamischer Tarif ---
// Greift nur wenn eco_score.json nicht verfuegbar ist (Dienst gestartet aber noch kein erster Zyklus).
// Preis kommt aus e3dc_v4.json (strompreis_basis), nicht mehr aus C++ e3dc.strompreise.txt.
$awattarMode = isset($confData['config']['awattar']) ? (int)$confData['config']['awattar'] : 0;
$useTiered = false;

if (!$useCache && ($awattarMode == 0 || $awattarMode == 2)) {
    $basisPreis = isset($confData['config']['strompreis_basis'])
        ? parseNumericConfigValue($confData['config']['strompreis_basis'], 0.0)
        : 0.0;
    if ($basisPreis > 0) {
        $prices = array_fill(0, 24, $basisPreis);
        $hNow = (int)date('G');
        $scheduledPrice = $basisPreis;
        $priceStartHour = 0;
        $priceInterval = 1.0;
        $minCt = $basisPreis; $maxCt = $basisPreis;
        $minSlot = '00.00'; $maxSlot = '00.00';
        $useTiered = true;
        $data['price_source'] = 'static_v4';
    }
}

// --- NEU: V4 Eco-Score Integration (Überschreibt Legacy e3dc.strompreise / awattardebug) ---
$ecoScoreFile = '/var/www/html/ramdisk/eco_score.json';
$useV4EcoScore = false;

if (file_exists($ecoScoreFile) && (time() - filemtime($ecoScoreFile) < 3600)) {
    $ecoScores = @json_decode(file_get_contents($ecoScoreFile), true);
    if ($ecoScores && is_array($ecoScores)) {
        $nowMs = time() * 1000;
        $pricesDay = array_fill(0, 24, 0.0);
        $foundMin = 999.0; $foundMax = -999.0;
        $minH = 0; $maxH = 0;

        foreach ($ecoScores as $score) {
            $h = (int)date('G', $score['start_timestamp'] / 1000);
            $p = (float)$score['billing_price'];
            $pricesDay[$h] = $p;

            if ($p < $foundMin) { $foundMin = $p; $minH = $h; }
            if ($p > $foundMax) { $foundMax = $p; $maxH = $h; }

            if ($nowMs >= $score['start_timestamp'] && $nowMs < $score['end_timestamp']) {
                $scheduledPrice = $p;
                $data['price_ct'] = $p;
                // V4 Extra State Transport in JSON
                $data['price_source'] = !empty($score['price_source']) ? (string)$score['price_source'] : 'V4_Eco_Manager';
                $data['price_resolution_min'] = $score['price_resolution_min'] ?? null;
                $data['price_source_resolution_min'] = $score['source_resolution_min'] ?? null;
                $data['optimization_score'] = $score['optimization_score'];
                $data['pure_eco_score'] = $score['pure_eco_score'];
            }
        }

        $prices = $pricesDay;
        $priceStartHour = 0;
        $priceInterval = 1.0;
        $minCt = $foundMin; $maxCt = $foundMax;
        $minSlot = str_pad($minH, 2, "0", STR_PAD_LEFT) . ".00";
        $maxSlot = str_pad($maxH, 2, "0", STR_PAD_LEFT) . ".00";

        // Disable Legacy Parsers
        $useV4EcoScore = true;
        $useCache = true;
        $useTiered = true;
    }
}

if (!$useCache && !$useTiered && !$useV4EcoScore) {
    $awData = parsePricesFromAwattarDebug($debugFile, $awmwst, $awnebenkosten, $speichergroesse);
    $scheduledPrice = $awData[0] ?? null;
    $minPrice = $awData[1] ?? null;
    $maxPrice = $awData[2] ?? null;
    $minCt = $awData[5] ?? null;
    $minSlot = $awData[6] ?? null;
    $maxCt = $awData[7] ?? null;
    $maxSlot = $awData[8] ?? null;
    $prices = $awData[9] ?? [];
    $priceStartHour = $awData[10] ?? null;
    $priceInterval = $awData[11] ?? 1.0;
    $forecast = $awData[12] ?? [];

    $cacheData = ['mtime' => $debugMtime, 'scheduledPrice' => $scheduledPrice, 'minPrice' => $minPrice, 'maxPrice' => $maxPrice, 'minCt' => $minCt, 'minSlot' => $minSlot, 'maxCt' => $maxCt, 'maxSlot' => $maxSlot, 'prices' => $prices, 'priceStartHour' => $priceStartHour, 'priceInterval' => $priceInterval, 'forecast' => $forecast];
    @file_put_contents($awattarCacheFile, json_encode($cacheData));
    @chmod($awattarCacheFile, 0664);
}

if ($minPrice !== null) {
    $data['price_min_ct'] = round($minCt, 2);
    $data['price_min_slot'] = $minSlot;
}
if ($maxPrice !== null) {
    $data['price_max_ct'] = round($maxCt, 2);
    $data['price_max_slot'] = $maxSlot;
}
$data['prices'] = $prices;
$data['price_start_hour'] = $priceStartHour;
$data['price_interval'] = $priceInterval;
$data['forecast'] = $forecast;

$validData = false; // Flag für gültige Daten

// ---------------------------------------------------------------------------
// LIVE-DATA QUELL-STEUERUNG (V4 Python Only)
// ---------------------------------------------------------------------------
$PY_LIVE_FILE = '/var/www/html/ramdisk/live_data_py.json';
$SHADOW_LIVE_DATA_FILE = '/var/www/html/ramdisk/shadow_master_live_data_py.json';
$SHADOW_STATUS_FILE = '/var/www/html/ramdisk/shadow_sync_status.json';
$liveSourceFile = $PY_LIVE_FILE;
$liveSourceLabel = 'live_data_py';
$liveData = null;
$shadowProjectionAllowed = !$isShadowMode;
$shadowStorageProjectionAllowed = !$isShadowMode;
$shadowWallboxBudgetProjectionAllowed = !$isShadowMode;

$data['_py_source'] = true;
$data['live_source'] = $liveSourceLabel;
$data['shadow_live_source'] = null;

if ($isShadowMode) {
    $data['shadow_only'] = true;
    $data['shadow_live_source'] = 'missing';
    $data['shadow_sync_status'] = null;
    $data['shadow_sync_reason'] = null;
    $data['shadow_master_url'] = null;
    $data['shadow_snapshot_age_s'] = null;
    $data['shadow_snapshot_max_age_s'] = null;
    $shadowStatus = null;
    $shadowMaxAgeS = 30.0;

    clearstatcache(true, $SHADOW_STATUS_FILE);
    if (file_exists($SHADOW_STATUS_FILE)) {
        $shadowStatus = @json_decode(file_get_contents($SHADOW_STATUS_FILE), true);
        if (is_array($shadowStatus)) {
            $data['shadow_sync_status'] = $shadowStatus['status'] ?? null;
            $data['shadow_sync_reason'] = $shadowStatus['reason'] ?? null;
            $data['shadow_master_url'] = $shadowStatus['master_url'] ?? null;
            $data['shadow_snapshot_age_s'] = $shadowStatus['snapshot_age_s'] ?? null;
            $data['shadow_snapshot_max_age_s'] = $shadowStatus['snapshot_max_age_s'] ?? null;
            if (
                isset($shadowStatus['snapshot_max_age_s'])
                && is_numeric($shadowStatus['snapshot_max_age_s'])
            ) {
                $shadowMaxAgeS = max(5.0, min(3600.0, (float)$shadowStatus['snapshot_max_age_s']));
            }
            $shadowStatusTs = isset($shadowStatus['ts']) && is_numeric($shadowStatus['ts'])
                ? (float)$shadowStatus['ts']
                : 0.0;
            $shadowStatusAgeS = time() - $shadowStatusTs;
            $shadowLiveTarget = $shadowStatus['targets']['live_data'] ?? null;
            $shadowProjectionAllowed = in_array(
                (string)($shadowStatus['status'] ?? ''),
                ['OK', 'WARN'],
                true
            )
                && $shadowStatusTs > 0
                && $shadowStatusAgeS >= -60.0
                && $shadowStatusAgeS <= $shadowMaxAgeS
                && is_array($shadowLiveTarget)
                && ($shadowLiveTarget['ok'] ?? false) === true
                && ($shadowLiveTarget['fresh'] ?? false) === true;
            $shadowStorageTarget = $shadowStatus['targets']['storage_state'] ?? null;
            $shadowStorageProjectionAllowed = $shadowProjectionAllowed
                && is_array($shadowStorageTarget)
                && ($shadowStorageTarget['ok'] ?? false) === true
                && ($shadowStorageTarget['fresh'] ?? false) === true;
            $shadowWallboxBudgetTarget = $shadowStatus['targets']['wb_budget'] ?? null;
            $shadowWallboxBudgetProjectionAllowed = $shadowProjectionAllowed
                && is_array($shadowWallboxBudgetTarget)
                && ($shadowWallboxBudgetTarget['ok'] ?? false) === true
                && ($shadowWallboxBudgetTarget['fresh'] ?? false) === true;
        }
    }

    if (!$shadowProjectionAllowed) {
        $shadowReason = (string)($data['shadow_sync_reason'] ?? '');
        $data['shadow_live_source'] = (
            str_contains($shadowReason, 'stale')
            || str_contains($shadowReason, 'timestamp')
        ) ? 'stale_preimage' : 'blocked_preimage';
    } else {
        clearstatcache(true, $SHADOW_LIVE_DATA_FILE);
    }
    if ($shadowProjectionAllowed && file_exists($SHADOW_LIVE_DATA_FILE)) {
        $shadowLiveData = @json_decode(file_get_contents($SHADOW_LIVE_DATA_FILE), true);
        $shadowPayloadTs = is_array($shadowLiveData)
            && array_key_exists('_ts', $shadowLiveData)
            && is_numeric($shadowLiveData['_ts'])
            ? (float)$shadowLiveData['_ts']
            : 0.0;
        if ($shadowPayloadTs > 10000000000.0) {
            $shadowPayloadTs /= 1000.0;
        }
        $shadowPayloadAgeS = time() - $shadowPayloadTs;
        if (
            is_array($shadowLiveData)
            && $shadowPayloadTs > 946684800.0
            && $shadowPayloadAgeS >= -60.0
            && $shadowPayloadAgeS <= $shadowMaxAgeS
            && isset($shadowLiveData['PV_Power'])
        ) {
            $liveData = $shadowLiveData;
            $liveSourceFile = $SHADOW_LIVE_DATA_FILE;
            $liveSourceLabel = 'shadow_master_live_data_py';
            $data['shadow_live_source'] = $liveSourceLabel;
        } else {
            $shadowProjectionAllowed = false;
            $shadowStorageProjectionAllowed = false;
            $shadowWallboxBudgetProjectionAllowed = false;
            $data['shadow_live_source'] = 'stale_preimage';
        }
    }
    if (!is_array($liveData)) {
        $shadowProjectionAllowed = false;
        $shadowStorageProjectionAllowed = false;
        $shadowWallboxBudgetProjectionAllowed = false;
        if ($data['shadow_live_source'] === 'missing') {
            $data['shadow_live_source'] = 'missing_preimage';
        }
    }
} else {
    clearstatcache(true, $PY_LIVE_FILE);
    if (file_exists($PY_LIVE_FILE)) {
        $liveData = @json_decode(file_get_contents($PY_LIVE_FILE), true);
    }
}
if (!is_array($liveData)) {
    $liveData = [];
}
$data['live_source'] = $liveSourceLabel;

// Key-Mapping: Python schreibt lowercase, PHP/Frontend erwartet teilweise PascalCase
if (isset($liveData['pv']) && !isset($liveData['PV_Power'])) {
    $liveData['PV_Power'] = (int)$liveData['pv'];
}
if (isset($liveData['bat']) && !isset($liveData['Battery_Power'])) {
    $liveData['Battery_Power'] = (int)$liveData['bat'];
}
if (isset($liveData['grid']) && !isset($liveData['Grid_Power'])) {
    $liveData['Grid_Power'] = (int)$liveData['grid'];
}
if (isset($liveData['home_raw']) && !isset($liveData['Home_Power'])) {
    $liveData['Home_Power'] = (int)$liveData['home_raw'];
}
if (isset($liveData['soc']) && !isset($liveData['SOC'])) {
    $liveData['SOC'] = (float)$liveData['soc'];
}
if (isset($liveData['wb']) && !isset($liveData['Wallbox_Power'])) {
    $liveData['Wallbox_Power'] = (float)$liveData['wb'];
}
if (isset($liveData['wp']) && !isset($liveData['WP_Power'])) {
    $liveData['WP_Power'] = (float)$liveData['wp'];
}
if (isset($liveData['heizstab_power']) && !isset($liveData['Heizstab_Power'])) {
    $liveData['Heizstab_Power'] = (int)$liveData['heizstab_power'];
}
if (isset($liveData['hs_power']) && !isset($liveData['Heizstab_Power'])) {
    $liveData['Heizstab_Power'] = (int)$liveData['hs_power'];
}
if (isset($liveData['ems_emergency_power_status']) && !isset($liveData['Notstrom_Status'])) {
    $liveData['Notstrom_Status'] = (int)$liveData['ems_emergency_power_status'];
}
if (isset($liveData['notstrom_status']) && !isset($liveData['Notstrom_Status'])) {
    $liveData['Notstrom_Status'] = (int)$liveData['notstrom_status'];
}

// Legacy: Wurzelzähler-Invertierung betrifft nur optionale PM-Phasendiagnose.
// Der native V5-Netzpunkt kommt direkt aus EMS Grid_Power und darf nicht gedreht werden.
$wurzelInvert = (isset($confData['config']['wurzelzaehler_invertiert']) && in_array((string)$confData['config']['wurzelzaehler_invertiert'], ['1', 'true', 'yes']));
if ($wurzelInvert && is_array($liveData)) {
    if (isset($liveData['grid_p1']) && is_numeric($liveData['grid_p1'])) $liveData['grid_p1'] = -1 * (float)$liveData['grid_p1'];
    if (isset($liveData['grid_p2']) && is_numeric($liveData['grid_p2'])) $liveData['grid_p2'] = -1 * (float)$liveData['grid_p2'];
    if (isset($liveData['grid_p3']) && is_numeric($liveData['grid_p3'])) $liveData['grid_p3'] = -1 * (float)$liveData['grid_p3'];
    $liveData['wurzelzaehler_invertiert_legacy'] = true;
}
// <---

// Sicherheits-Check: Nur verarbeiten, wenn das JSON gültig ist und PV_Power enthält
if (is_array($liveData) && isset($liveData['PV_Power'])) {
        $mtime = file_exists($liveSourceFile) ? filemtime($liveSourceFile) : 0;

        // --- Heizstab Manager (wp_type=2 + wp_type=3) ---
        // Dritte Datenquelle: heizstab_manager.py schreibt heizstab_data.json alle ~10s.
        // wp_type=2: Shelly Heizstab / Heizluefter
        // wp_type=3: Shelly Pro3EM (WP-Monitoring ohne native Anbindung)
// BEIDE Typen laufen über heizstab_manager.py und schreiben heizstab_data.json!
        $HS_DATA_FILE = '/var/www/html/ramdisk/heizstab_data.json';
        $hsData = [];
        if (file_exists($HS_DATA_FILE) && (time() - filemtime($HS_DATA_FILE)) < 60) {
            $hsData = @json_decode(file_get_contents($HS_DATA_FILE), true);
            if (is_array($hsData) && ($hsData['success'] ?? false)) {
                // Heizstab/myPV live einspielen. Shelly-Leistung ist die echte
                // Messung, ELWA liefert geschaetzte Istleistung aus Status+Sollwert.
                $hsPower = isset($hsData['Heizstab_Power']) ? (float)$hsData['Heizstab_Power'] : 0.0;
                if (!empty($hsData['shelly_heiz_on']) && isset($hsData['shelly_heiz_w'])) {
                    $hsPower = max($hsPower, (float)$hsData['shelly_heiz_w']);
                }
                $liveData['Heizstab_Power'] = (int)round(max(0, $hsPower));
                $data['hs_power'] = $liveData['Heizstab_Power'];

                // wp_type=3: WP-Leistung aus Pro3EM als WP-Blase setzen
                if ($wpTypeCfg === 3 && isset($hsData['wp_power_w'])) {
                    $data['wp'] = (int)$hsData['wp_power_w'];
                }
// Statusfelder für UI-Dekoration und Diagnose.
                foreach (['heizstab_type','hs_actual_w','hs_target_w','hs_requested_w','hs_active','shelly_heiz_on',
                          'shelly_heiz_w','hs_mode','hs_reason','surplus_w','grid_surplus_w',
                          'predump_heater_active','elwa_status','elwa_status_code','elwa_setpoint_w',
                          'elwa_water_temp_c','elwa_target_temp_c'] as $k) {
                    if (isset($hsData[$k])) {
                        $liveData[$k] = $hsData[$k];
                        $data[$k] = $hsData[$k];
                    }
                }
            }
        }

        // --- Klimaanlage als eigener gemessener Zusatzverbraucher ---
        // climate_live.py liest nur den eigenen Shelly-Zähler. Der Wert wird
        // separat geführt und später aus dem bereinigten Hausverbrauch gezogen.
        $CLIMATE_LOAD_FILE = '/var/www/html/ramdisk/climate_load.json';
        if (file_exists($CLIMATE_LOAD_FILE) && (time() - filemtime($CLIMATE_LOAD_FILE)) < 120) {
            $climateData = @json_decode(file_get_contents($CLIMATE_LOAD_FILE), true);
            if (is_array($climateData) && !empty($climateData['enabled'])) {
                $climatePower = isset($climateData['power_w']) && is_numeric($climateData['power_w'])
                    ? max(0, (float)$climateData['power_w'])
                    : 0.0;
                $data['climate_power_w'] = (int)round($climatePower);
                $data['climate_active'] = !empty($climateData['active']);
                $data['climate_online'] = !empty($climateData['online']);
                $data['climate_source'] = (string)($climateData['source'] ?? 'climate_live');
                $data['climate_name'] = (string)($climateData['name'] ?? 'Klimaanlage');
                $data['climate_phase'] = (string)($climateData['phase'] ?? '');
                $data['climate_meter_age_s'] = e3dcLiveMeasurementAgeSeconds(
                    $climateData['ts'] ?? null,
                    microtime(true)
                );
                $climatePollS = isset($confData['config']['climate_poll_s'])
                    && is_numeric($confData['config']['climate_poll_s'])
                    ? max(5, (int)$confData['config']['climate_poll_s'])
                    : 15;
                $climateFrequencyMaxAgeS = max(30, min(120, 3 * $climatePollS));
                $climateFrequency = e3dcLiveFrequencyProjection(
                    $climateData['freq_hz'] ?? null,
                    e3dcLiveMeasurementConfirmed(
                        $climateData['measurement_valid'] ?? null,
                        $climateData['online'] ?? null
                    ),
                    'climate_meter:' . (string)($climateData['source'] ?? 'unknown'),
                    $data['climate_meter_age_s'],
                    $climateFrequencyMaxAgeS
                );
                if (!$data['grid_frequency_valid'] && $climateFrequency['valid']) {
                    $data['grid_frequency_hz'] = $climateFrequency['frequency_hz'];
                    $data['grid_frequency_valid'] = true;
                    $data['grid_frequency_source'] = $climateFrequency['source'];
                    $data['grid_frequency_age_s'] = $climateFrequency['age_s'];
                }
                if (isset($climateData['daily_kwh']) && is_numeric($climateData['daily_kwh'])) {
                    $data['climate_daily_kwh'] = round((float)$climateData['daily_kwh'], 3);
                }
                if (isset($climateData['energy_total_kwh']) && is_numeric($climateData['energy_total_kwh'])) {
                    $data['climate_total_kwh'] = round((float)$climateData['energy_total_kwh'], 3);
                }
                foreach (['current_a','voltage_v','pf','apparent_power_va','raw_power_w','min_power_w','control_enabled','control_mode'] as $k) {
                    if (isset($climateData[$k])) {
                        $data['climate_' . $k] = $climateData[$k];
                    }
                }
                $liveData['Climate_Power'] = $data['climate_power_w'];
            }
        }

        $CLIMATE_CONTROL_FILE = '/var/www/html/ramdisk/climate_control.json';
        if (file_exists($CLIMATE_CONTROL_FILE) && (time() - filemtime($CLIMATE_CONTROL_FILE)) < 180) {
            $climateControl = @json_decode(file_get_contents($CLIMATE_CONTROL_FILE), true);
            if (is_array($climateControl) && (!empty($climateControl['prepared']) || !empty($climateControl['read_only']))) {
                $data['climate_control_prepared'] = true;
                $data['climate_control_enabled'] = !empty($climateControl['enabled']);
                $data['climate_control_read_only'] = !empty($climateControl['read_only']);
                $data['climate_control_active'] = !empty($climateControl['active']);
                $data['climate_control_ready'] = !empty($climateControl['control_ready']);
                $data['climate_control_commands_allowed'] = !empty($climateControl['commands_allowed']);
                $data['climate_control_reason'] = (string)($climateControl['reason'] ?? '');
                $data['climate_control_provider'] = (string)($climateControl['provider'] ?? '');
                $data['climate_control_mode'] = (string)($climateControl['mode'] ?? '');
                $data['climate_cloud_connected'] = !empty($climateControl['cloud_connected']);
                $data['climate_cloud_device_count'] = isset($climateControl['cloud_device_count'])
                    ? (int)$climateControl['cloud_device_count']
                    : 0;
                $data['climate_configured_device_count'] = isset($climateControl['configured_device_count'])
                    ? (int)$climateControl['configured_device_count']
                    : 0;
                if (isset($climateControl['unmatched_device_ids']) && is_array($climateControl['unmatched_device_ids'])) {
                    $data['climate_unmatched_device_ids'] = $climateControl['unmatched_device_ids'];
                }
                foreach (['room_temp_c','outside_temp_c','target_temp_c','ac_status','ac_mode','fan','primary_device_name'] as $k) {
                    if (isset($climateControl[$k])) {
                        $data['climate_' . $k] = $climateControl[$k];
                    }
                }
                if (isset($climateControl['devices']) && is_array($climateControl['devices'])) {
                    $data['climate_cloud_devices'] = $climateControl['devices'];
                }
                if (isset($climateControl['schedule']) && is_array($climateControl['schedule'])) {
                    $data['climate_control_schedule'] = $climateControl['schedule'];
                }
            }
        }

        $now = time();

        // --- Stale-Data Detection (Einfrier-Schutz) ---
        // (Legacy System: Wurde für C++ Screen-Scraping genutzt.
        //  Python RSCP ist atomar und friert nicht ein, daher entfernt um falsche Offline-Meldungen zu verhindern.)
        // --- Ende Stale-Data Detection ---

        $data['time'] = date("H:i:s", $mtime);
        $data['ts'] = $mtime;

        foreach ([
            'ems_max_charge_power_w',
            'ems_max_discharge_power_w',
            'ems_discharge_start_power_w',
            'used_charge_limit_w',
            'user_charge_limit_w',
            'bat_charge_limit_w',
            'used_discharge_limit_w',
        ] as $emsLimitKey) {
            if (isset($liveData[$emsLimitKey]) && is_numeric($liveData[$emsLimitKey])) {
                $data[$emsLimitKey] = $liveData[$emsLimitKey] + 0;
            }
        }
        foreach (['power_limits_active', 'ems_power_settings_read'] as $emsLimitFlag) {
            if (array_key_exists($emsLimitFlag, $liveData)) {
                $data[$emsLimitFlag] = !empty($liveData[$emsLimitFlag]);
            }
        }


        // Filter-Logik initialisieren
        $filterFile = '/var/www/html/ramdisk/value_filter.json';
        $fState = file_exists($filterFile) ? (json_decode(file_get_contents($filterFile), true) ?: []) : [];

        $process = function($k, $v) use (&$fState) {
            if (!isset($fState[$k])) $fState[$k] = ['last' => $v, 'z' => 0];
            if ($v == 0) {
                if ($fState[$k]['z'] < 6) {
                    $fState[$k]['z']++;
                    return $fState[$k]['last'];
                }
                $fState[$k]['last'] = 0;
            } else {
                $fState[$k]['last'] = $v;
                $fState[$k]['z'] = 0;
            }
            return $v;
        };

        // --- DIAGNOSE: PM-PHASEN UND WALLBOX-PHASEN ---
        // PM-Netzphasen bleiben reine Grid-Health-Diagnose. Der fuehrende
// Netzpunkt für Anzeige, Historie und Regelung bleibt EMS Grid_Power.
// Nur die Wallbox-Phasensumme wird für den WB-Fallback genutzt.
        $pm_wb_sum   = (float)($liveData['wb_p1']??0) + (float)($liveData['wb_p2']??0) + (float)($liveData['wb_p3']??0);
        $gridVal = (int)($liveData['Grid_Power'] ?? 0);
        $wbVal_e3dc = abs($pm_wb_sum) > 5 ? (int)round($pm_wb_sum) : (int)($liveData['Wallbox_Power'] ?? $liveData['Wb_Power'] ?? 0);

        $raw_pv = (int)($liveData['PV_Power'] ?? 0);
        $externalPowerReported = array_key_exists('Ext_PV_Power', $liveData);
        $externalPowerValid = $externalPowerReported
            && liveBoolValue($liveData['Ext_PV_Power_Valid'] ?? false, false)
            && is_numeric($liveData['Ext_PV_Power']);
        $raw_ext_pv = $externalPowerValid ? max(0, (int)$liveData['Ext_PV_Power']) : 0;
        $data['pv_external_power_valid'] = $externalPowerValid;
        $data['pv_external_power_age_s'] = $externalPowerValid && isset($liveData['Ext_PV_Power_Age_S']) && is_numeric($liveData['Ext_PV_Power_Age_S'])
            ? max(0.0, (float)$liveData['Ext_PV_Power_Age_S'])
            : null;
        $raw_ext_pv = min($raw_ext_pv, max(0, $raw_pv));
        $raw_bat = (int)($liveData['Battery_Power'] ?? $liveData['Bat_Power'] ?? 0);
        $raw_home_rscp = (int)($liveData['Home_Power'] ?? $liveData['Haus_Power'] ?? 0);
        $raw_home = $raw_home_rscp;
        $homePowerValid = liveBoolValue($liveData['Home_Power_Valid'] ?? true, true);
        $gridPowerValid = liveBoolValue($liveData['Grid_Power_Valid'] ?? true, true);
        $rscpSampleValid = liveBoolValue($liveData['RSCP_Sample_Valid'] ?? true, true);
        $homePowerSource = (string)($liveData['Home_Power_Source'] ?? 'legacy_unmarked');
        $homePowerIndependent = liveBoolValue($liveData['Home_Power_Independent'] ?? true, true);
        $rscpGlitchReasons = [];
        if (isset($liveData['RSCP_Glitch_Reasons']) && is_array($liveData['RSCP_Glitch_Reasons'])) {
            $rscpGlitchReasons = array_values(array_filter(array_map('strval', $liveData['RSCP_Glitch_Reasons'])));
        }

        // --- ANTI-GLITCH: PHYSIKALISCHER HAUSVERBRAUCH FALLBACK ---
        // Gleichung: PV + Grid(Bezug=Pos) = Home + Bat(Laden=Pos) + Wallbox
        // Umgestellt: Home = PV + Grid - Bat - Wallbox
        $calcHome = isset($liveData['Home_Power_Balance']) && is_numeric($liveData['Home_Power_Balance'])
            ? (float)$liveData['Home_Power_Balance']
            : ($raw_pv + $gridVal - $raw_bat - $wbVal_e3dc);
        $homeDelta = isset($liveData['Home_Power_Delta']) && is_numeric($liveData['Home_Power_Delta'])
            ? (float)$liveData['Home_Power_Delta']
            : ($raw_home_rscp - $calcHome);
        if (!$homePowerValid && $gridPowerValid && $calcHome > 50 && $calcHome < 20000) {
            // Anzeige aktuell halten, den echten Rohwert aber als home_rscp_raw markieren.
            $raw_home = (int)round($calcHome);
            if ($homePowerSource === '' || $homePowerSource === 'legacy_unmarked') {
                $homePowerSource = 'invalid_home_balance_display';
            }
        } elseif ($homePowerValid && ($raw_home < 50 || abs($raw_home - $calcHome) > 2000)) {
// Legacy-Fallback für Installationen ohne neue e3dc_live-Flags.
            if ($calcHome > 50 && $calcHome < 20000) {
                $raw_home = (int)round($calcHome);
                $homePowerSource = 'legacy_energy_balance';
            }
        }

        $data['pv']      = $process('pv', $raw_pv);
        $data['pv_external_w'] = $externalPowerValid
            ? min(max(0, $raw_ext_pv > 0 ? (int)$process('pv_external', $raw_ext_pv) : 0), max(0, (int)$data['pv']))
            : 0;
        $data['pv_total_w'] = (int)$data['pv'];
        $data['pv_e3dc_w'] = max(0, (int)$data['pv'] - (int)$data['pv_external_w']);
        $data['pv_external_source'] = $externalPowerValid
            ? (string)($liveData['Ext_PV_Power_Source'] ?? 'e3dc_add_power')
            : ($externalPowerReported ? 'invalid' : 'not_reported');
        // Der Python-RSCP-Writer veröffentlicht den gesamten Live-Frame atomar.
        // Ein echter Batterieübergang auf 0 W darf deshalb nicht mit einem
        // früheren Lade-/Entladewert vermischt werden: Sonst widersprechen sich
        // Batterie, Netzpunkt und Summenbilanz im selben Energiefluss-Frame.
        $data['bat']     = $raw_bat;
        $data['home_raw']= $process('home', $raw_home);
        $data['home_rscp_raw'] = $raw_home_rscp;
        $data['home_balance'] = is_numeric($calcHome) ? (int)round($calcHome) : null;
        $data['home_delta'] = is_numeric($homeDelta) ? (int)round($homeDelta) : null;
        $data['home_power_source'] = $homePowerSource;
        $data['home_power_valid'] = $homePowerValid;
        $data['home_power_independent'] = $homePowerIndependent;
        $data['grid_power_valid'] = $gridPowerValid;
        $data['rscp_sample_valid'] = $rscpSampleValid && $homePowerValid && $gridPowerValid && empty($rscpGlitchReasons);
        $data['rscp_glitch_reasons'] = $rscpGlitchReasons;
        $data['grid']    = $process('grid', $gridVal);

        file_put_contents($filterFile, json_encode($fState), LOCK_EX);
        @chmod($filterFile, 0666);

        // SOC: Glitch-Schutz gegen C++ Sentinel-Wert -1 ("Batterie kurz nicht lesbar").
        // Wenn Python aktiv ist, kommt SOC von dort (immer gueltig 0-100).
        // Falls nur C++ verfuegbar: Wert ausserhalb 0-100 auf letzten bekannten Wert halten.
        $rawSoc = (float)($liveData['SOC'] ?? -1);
        $data['soc'] = ($rawSoc >= 0.0 && $rawSoc <= 100.0) ? $rawSoc : ($data['soc'] ?: 0.0);
        clearstatcache(true, $liveSourceFile);
        $houseSocSourceTs = is_file($liveSourceFile) ? (int)@filemtime($liveSourceFile) : 0;
        $data['house_battery_soc'] = [
            'value' => $data['soc'],
            'source' => $liveSourceLabel,
            'source_ts' => $houseSocSourceTs > 0 ? $houseSocSourceTs : null,
            'age_s' => $houseSocSourceTs > 0 ? max(0, time() - $houseSocSourceTs) : null,
            'domain' => 'house_battery',
        ];
        $data['wb'] = (float)$wbVal_e3dc;
        if (isset($liveData['Heizstab_Power'])) {
            $data['hs_power'] = (int)$liveData['Heizstab_Power'];
        }

        // --- openWB Native: Status aus openwb_data.json (geschrieben vom wallbox_manager) ---
        $openwbDataFile = '/var/www/html/ramdisk/openwb_data.json';
        $wbNativeType = strtolower(trim($confData['config']['wb_native_type'] ?? ''));
        if (($wbNativeType === 'openwb' || $wbNativeType === 'openwb_pro')
            && file_exists($openwbDataFile)
            && (time() - filemtime($openwbDataFile) < 30)) {
            $openwbData = @json_decode(file_get_contents($openwbDataFile), true);
            if (is_array($openwbData)) {
                $data['wb_status_fresh'] = true;
                // Messwerte
                $owbPower = (float)($openwbData['power_w'] ?? 0);
                if ($owbPower >= 0) $data['wb'] = $owbPower;  // openWB-Messung hat Vorrang
                $data['wb_p1'] = (float)($openwbData['phase_power_l1_w'] ?? 0);
                $data['wb_p2'] = (float)($openwbData['phase_power_l2_w'] ?? 0);
                $data['wb_p3'] = (float)($openwbData['phase_power_l3_w'] ?? 0);
                $data['wb_kva'] = liveWallboxApparentKva($openwbData);
                $data['wb_power_factor'] = liveWallboxPowerFactor($openwbData, $owbPower);
                $data['wb_set_amp'] = (float)($openwbData['current_set_amp'] ?? $openwbData['amp'] ?? 0);
                $data['wb_cap_amp'] = (float)($openwbData['cap_amp'] ?? $openwbData['target_amp'] ?? 0);
                $data['wb_status_amp'] = (float)($openwbData['status_amp'] ?? $openwbData['amp'] ?? 0);
                liveApplyWallboxFineAmpFields($data, 'wb', $openwbData, $data['wb_set_amp']);
                $wbSocSource = (string)($openwbData['car_soc_source'] ?? '');
                $wbSocSourceTs = wallboxSocSourceTimestamp(
                    $openwbData,
                    $wbSocSource
                );
                $wbSocValue = vehicleSocPercentValue($openwbData['car_soc'] ?? null);
                $wbSocConfirmed = $wbSocValue !== null
                    && !wallboxSocPayloadExplicitVetoed($openwbData)
                    && wallboxSocTruthConfirmed(
                    $wbSocSource,
                    $openwbData['car_soc_rule_confirmed'] ?? null,
                    $wbSocSourceTs,
                    null,
                    array_key_exists('car_soc_rule_confirmed', $openwbData),
                    $openwbData
                );

// Status-Felder für UI: SoC nur anzeigen, wenn die Quelle regelbestätigt ist.
                $data['wb_soc']             = $wbSocConfirmed ? $wbSocValue : 0.0;
                $data['wb_soc_source']      = $wbSocSource;
                $data['wb_soc_source_ts']   = $wbSocSourceTs;
                $data['wb_soc_rule_confirmed'] = $wbSocConfirmed;
                $data['wb_soc_age_contract'] = $openwbData['car_soc_age_contract'] ?? null;
                $data['wb_soc_age_contract_source'] = $openwbData['car_soc_age_contract_source'] ?? null;
                $data['wb_soc_max_age_s'] = $openwbData['car_soc_max_age_s'] ?? null;
                $wbRangeContract = openwbTotalRangeContract($openwbData);
                $data['wb_range'] = is_array($wbRangeContract)
                    ? (float)$wbRangeContract['range_km']
                    : 0.0;
                foreach ([
                    'car_range_source' => 'wb_range_source',
                    'car_range_valid' => 'wb_range_valid',
                    'car_range_observed_ts' => 'wb_range_observed_ts',
                    'car_range_source_ts' => 'wb_range_source_ts',
                    'car_range_source_ts_explicit' => 'wb_range_source_ts_explicit',
                    'car_range_vehicle_key' => 'wb_range_vehicle_key',
                ] as $contractKey => $dataKey) {
                    $data[$dataKey] = is_array($wbRangeContract)
                        ? $wbRangeContract[$contractKey]
                        : null;
                }
                $data['wb_charged_range']   = $wbSocConfirmed ? (float)($openwbData['car_charged_range'] ?? $openwbData['range_charged'] ?? 0) : 0.0;
                $data['wb_plug']            = liveBoolValue($openwbData['plug_state'] ?? false);
                $data['wb_locked']          = liveBoolValue(
                    $openwbData['locked'] ?? $openwbData['lock_state'] ?? $openwbData['plug_locked'] ?? null,
                    $data['wb_plug']
                );
                $data['wb_session_kwh']     = round((float)($openwbData['session_kwh'] ?? 0), 2);
                $wbDaily = normalizeOpenwbDailyKwh(
                    1,
                    (float)($openwbData['daily_imported_wh'] ?? 0),
                    $owbPower,
                    (bool)($openwbData['charge_state'] ?? false),
                    $data['wb_session_kwh']
                );
                $data['wb_daily_kwh']       = round((float)$wbDaily['kwh'], 2);
                $data['wb_daily_raw_kwh']   = $wbDaily['raw_kwh'];
                $data['wb_daily_base_kwh']  = $wbDaily['baseline_kwh'];
                $data['wb_chargemode']      = $openwbData['chargemode'] ?? 'stop';
                $data['wb_source']          = 'openwb_native';
                $data['wb_chargepoint_name'] = $openwbData['chargepoint_name'] ?? '';
                $data['wb_state_text'] = $openwbData['state_text'] ?? '';
                $data['wb_fault_text'] = $openwbData['fault_text'] ?? '';
                $data['wb_phases']          = (int)($openwbData['phases_in_use'] ?? 0);
                $data['wb_phases_actual']   = (int)($openwbData['phases_actual'] ?? 0);
                $data['wb_phases_target']   = (int)($openwbData['phases_target'] ?? 0);
                $data['wb_can_switch_phases'] = (bool)($openwbData['can_switch_phases'] ?? false);
                $data['wb_phase_switch_capability'] = $openwbData['phase_switch_capability'] ?? '';
                $data['wb_phase_switch_source'] = $openwbData['phase_switch_source'] ?? '';
                $data['wb_api_surface'] = $openwbData['api_surface'] ?? '';
                $data['wb_control_status'] = $openwbData['control_status'] ?? '';
                $data['wb_control_label'] = $openwbData['control_label'] ?? '';
                $data['wb_control_detail'] = $openwbData['control_detail'] ?? '';
                $data['wb_control_level'] = $openwbData['control_level'] ?? '';
                $data['wb_last_command_ok'] = $openwbData['last_command_ok'] ?? null;
                $data['wb_last_command_amp'] = $openwbData['last_command_amp'] ?? null;
                $data['wb_last_heartbeat_ok'] = $openwbData['last_heartbeat_ok'] ?? null;
                $data['wb_configured_role'] = $openwbData['configured_role'] ?? '';
                $data['wb_detected_role'] = $openwbData['detected_role'] ?? '';
                $data['wb_effective_role'] = $openwbData['effective_role'] ?? '';
                $data['wb_role_mismatch'] = (bool)($openwbData['role_mismatch'] ?? false);
                $data['wb_command_failure_count'] = (int)($openwbData['command_failure_count'] ?? 0);
                $data['wb_command_failure_limit'] = (int)($openwbData['command_failure_limit'] ?? 3);
                $data['wb_command_blocked'] = (bool)($openwbData['command_blocked'] ?? false);
                $data['wb_evse_a']          = round((float)($openwbData['evse_current'] ?? 0), 1);
                $data['wb_cp_id']           = (int)($openwbData['cp_id'] ?? 3);
                $data['wb_native_ip']       = $confData['config']['wb_native_ip'] ?? '';
                $data['wb_soc']             = $wbSocConfirmed ? $wbSocValue : 0.0;
                $data['wb_soc_source']      = $wbSocSource;
                $data['wb_soc_rule_confirmed'] = $wbSocConfirmed;
                $data['wb_charging']        = (bool)($openwbData['charge_state'] ?? false) || $owbPower > 50;
                $data['wb_charge_profile_name'] = trim((string)($openwbData['charge_template_name'] ?? ''));
                $data['wb_charge_profile_source'] = $data['wb_charge_profile_name'] !== '' ? 'openwb_charge_template' : '';
                $wbIdentityCurrent = array_key_exists('stable_vehicle_identity_current', $openwbData)
                    ? (bool)$openwbData['stable_vehicle_identity_current']
                    : false;
                $wbHasCurrentVehicle = !empty($data['wb_plug']) || !empty($data['wb_charging']) || $owbPower > 50;
                if ($wbHasCurrentVehicle) {
                    $data['wb_car_name']        = trim((string)($openwbData['car_name'] ?? ''));
                    $data['wb_car_id']          = $openwbData['car_id'] ?? null;
                    $data['wb_vehicle_id']      = $openwbData['vehicle_id'] ?? null;
                    $data['wb_rfid_tag']        = $openwbData['rfid_tag'] ?? null;
                    $data['wb_rfid_timestamp']  = $openwbData['rfid_timestamp'] ?? null;
                    $data['wb_live_car_name']   = $data['wb_car_name'];
                    $data['wb_live_car_id']     = $data['wb_car_id'];
                    $data['wb_live_vehicle_id'] = $data['wb_vehicle_id'];
                    $data['wb_live_rfid_tag']   = $data['wb_rfid_tag'];
                    $data['wb_vehicle_identity_current'] = $wbIdentityCurrent;
                    $data['wb_stable_vehicle_identity_current'] = $wbIdentityCurrent;
                    $wbDisplayVehicle = resolveWallboxDisplayVehicle(
                        true,
                        $wbIdentityCurrent,
                        $data['wb_car_name'],
                        $data['wb_charge_profile_name']
                    );
                    $data['wb_display_car_name'] = $wbDisplayVehicle['name'];
                    $data['wb_display_car_source'] = $wbDisplayVehicle['source'];
                } else {
                    $data['wb_car_name']        = '';
                    $data['wb_car_id']          = null;
                    $data['wb_vehicle_id']      = null;
                    $data['wb_rfid_tag']        = null;
                    $data['wb_rfid_timestamp']  = null;
                    $data['wb_live_car_name']   = '';
                    $data['wb_live_car_id']     = null;
                    $data['wb_live_vehicle_id'] = null;
                    $data['wb_live_rfid_tag']   = null;
                    $data['wb_vehicle_identity_current'] = false;
                    $data['wb_stable_vehicle_identity_current'] = false;
                    $data['wb_display_car_name'] = '';
                    $data['wb_display_car_source'] = '';
                }
                $data['wb_pro_serial']      = $openwbData['serial'] ?? null;
                $data['wb_pro_temp_c']      = $openwbData['temp_c'] ?? null;

                // Tagesenergie: openWB daily_imported ist praeziser als C++ Wallbox_Energy_kWh
                liveSetExactCounter($data, 'e_wb', $data['wb_daily_kwh'], $wbDaily['source'], 90);
            }
        }

        // --- Native Wallbox Fallback (E3DC Native etc.): Status aus wb_live_session.json (geschrieben vom wallbox_manager per RSCP) ---
        $wbNativeEnable = in_array(strtolower(trim($confData['config']['wb_native_enable'] ?? '0')), ['1', 'true']);
        $wbLiveSessionFile = e3dcFirstFreshRegularFile([
            '/var/www/html/ramdisk/wb_live_session.json',
            '/var/www/html/logs/wb_live_session.json',
        ], 15.0);
        if ($wbConfigured && $wbNativeType !== 'openwb' && $wbNativeType !== 'openwb_pro' && $wbNativeEnable && is_string($wbLiveSessionFile)) {
            $wbLiveSession = @json_decode(file_get_contents($wbLiveSessionFile), true);
            if (is_array($wbLiveSession)) {
                $carConnected = (bool)($wbLiveSession['car_connected'] ?? false);
                if (isset($wbLiveSession['power_w'])) {
                    $nativePwr = abs((float)$wbLiveSession['power_w']);
                    $nativePowerSource = strtolower((string)($wbLiveSession['power_source'] ?? ''));
                    $nativeLastPowerTs = (int)($wbLiveSession['last_power_ts'] ?? 0);
                    $nativePowerAge = $nativeLastPowerTs > 0 ? (time() - $nativeLastPowerTs) : 9999;
                    $nativePowerAccepted = (
                        $nativePwr > 50
                        && (
                            ($nativePowerSource === 'rscp_real' && $nativePowerAge <= 20)
                            || ($nativePowerSource === 'glitch_hold' && $nativePowerAge <= 8)
                        )
                    );
                    $data['wb_live_session_power_source'] = $nativePowerSource;

                    // Leistungs-Hold: Kurze 0W-Aussetzer (RSCP-Glitch) nicht ans UI weitergeben
                    // Letzten gueltigen Wert aus der Session-State-Datei lesen
                    $wbPwrCacheFile = '/var/www/html/ramdisk/wb_native_pwr_cache.json';
                    $pwrCache = [];
                    if (file_exists($wbPwrCacheFile)) {
                        $pwrCache = @json_decode(file_get_contents($wbPwrCacheFile), true) ?: [];
                    }
                    if ($nativePowerAccepted) {
                        // Gueltiger Wert: Im Cache speichern
                        $pwrCache = ['power_w' => $nativePwr, 'ts' => time()];
                        @file_put_contents($wbPwrCacheFile, json_encode($pwrCache));
                    } elseif (
                        $carConnected
                        && $nativePowerSource === 'glitch_hold'
                        && $nativePowerAge <= 8
                        && !empty($pwrCache['power_w'])
                        && (time() - ($pwrCache['ts'] ?? 0)) < 12
                    ) {
// 0W-Aussetzer während aktivem Ladebit: Letzten Wert kurz einfrieren.
                        $nativePwr = (float)$pwrCache['power_w'];
                    } else {
                        @file_put_contents($wbPwrCacheFile, json_encode(['power_w' => 0, 'ts' => time()]));
                        $nativePwr = 0.0;
                    }

                    if ($nativePwr > 0 || $carConnected) {
                        $data['wb'] = $nativePwr;
                    }
                }
                // RSCP Session-kWh direkt ans Frontend weitergeben (verhindert Integrations-Fehler)
                if (($wbLiveSession['source'] ?? '') === 'rscp' && $carConnected) {
                    $rscp_session_kwh = (float)($wbLiveSession['session_kwh'] ?? 0);
                    if ($rscp_session_kwh >= 0) {
                        $data['python_wb1_session_kwh'] = round($rscp_session_kwh, 3);
                        $data['wb_plug'] = true;  // Auto ist per RSCP bestätigt angesteckt
                    }
                }
            }
        }

        if (isset($liveData['WP_Power'])) {
            $wp_pwr = (float)$liveData['WP_Power'];
            // Ghost-Filter: Eba-M's C++ Kern schreibt oft Fehlwerte (z.B. Außentemperatur Register 1000 = 912W) in WP_Power.
            // Wenn eine native Integration (IDM/Luxtronik) konfiguriert ist, ignorieren wir E3DC-WP-Power komplett!
            $wpType = $wpTypeCfg;
            $luxtronikOn = isset($confData['config']['luxtronik']) && in_array(strtolower(trim($confData['config']['luxtronik'])), ['1', 'true']);

            if ($wpType == 0 && !$luxtronikOn) {
// Auto-Healing für Eba-M's Bug (Strompreis * 1000)
                $fakeExpected = isset($scheduledPrice) ? $scheduledPrice * 1000 : 0;
                if ($fakeExpected > 100 && abs($wp_pwr - $fakeExpected) < ($fakeExpected * 0.05)) {
                    $wp_pwr = 0;
                }
                if ($wp_pwr > 30000) $wp_pwr = 0;
                $data['wp'] = $wp_pwr;
            } elseif ($wpType === 3) {
                // wp_type=3 (Shelly Pro3EM): Wert kommt aus heizstab_data.json (oben bereits gesetzt).
                // data['wp'] wurde bereits korrekt befuellt - NICHT ueberschreiben!
// Nur anzeigen wenn WP wirklich läuft (wp_is_running aus heizstab_data.json).
                // Filtert die Grundlast der Unterverteilung (typisch 50-75W) heraus.
                if (!empty($hsData['wp_is_running'])) {
                    // wp_is_running: heizstab_manager setzt dies wenn total_w >= s3_min_w * 0.3
                    // data['wp'] bleibt wie gesetzt (enthält echte WP-Last)
                } else {
                    $data['wp'] = 0; // Standby / Grundlast - nicht als WP-Verbrauch zaehlen
                }
            } else {
                // Bei IDM/Luxtronik: Kern-Wert ignorieren (wird spaeter aus JSON befuellt)
                $data['wp'] = 0;
            }
        }

        $luxtronikEnabled = isset($confData['config']['luxtronik']) && in_array(strtolower(trim($confData['config']['luxtronik'])), ['1', 'true']);

        // Exakte Tageszähler aus C++

        if (isset($liveData['PV_Energy_kWh'])) $data['e_pv'] = round((float)$liveData['PV_Energy_kWh'], 3);
        if (isset($liveData['Grid_In_Energy_kWh'])) $data['e_grid_in'] = round((float)$liveData['Grid_In_Energy_kWh'], 3);
        if (isset($liveData['Grid_Out_Energy_kWh'])) $data['e_grid_out'] = round((float)$liveData['Grid_Out_Energy_kWh'], 3);
        if (isset($liveData['Bat_In_Energy_kWh'])) $data['e_bat_in'] = round((float)$liveData['Bat_In_Energy_kWh'], 3);
        if (isset($liveData['Bat_Out_Energy_kWh'])) $data['e_bat_out'] = round((float)$liveData['Bat_Out_Energy_kWh'], 3);
        if (isset($liveData['Home_Energy_kWh'])) $data['e_home'] = round((float)$liveData['Home_Energy_kWh'], 3);
        $wbEnergy = $liveData['Wallbox_Energy_kWh'] ?? $liveData['Wb_Energy_kWh'] ?? $liveData['WB_Energy_kWh'] ?? null;
        if ($wbEnergy !== null) liveSetExactCounter($data, 'e_wb', round((float)$wbEnergy, 3), 'live_wallbox_energy', 40);
        if (!$luxtronikEnabled && isset($liveData['WP_Energy_kWh'])) {
            $data['e_wp'] = round((float)$liveData['WP_Energy_kWh'], 3);
        }

        $data['dc0_w'] = (int)($liveData['dc0_w'] ?? 0); $data['dc0_v'] = round((float)($liveData['dc0_v'] ?? 0), 2); $data['dc0_a'] = round((float)($liveData['dc0_a'] ?? 0), 2);
        $data['dc1_w'] = (int)($liveData['dc1_w'] ?? 0); $data['dc1_v'] = round((float)($liveData['dc1_v'] ?? 0), 2); $data['dc1_a'] = round((float)($liveData['dc1_a'] ?? 0), 2);

        $data['ac0_w'] = (int)($liveData['ac0_w'] ?? 0); $data['ac0_v'] = round((float)($liveData['ac0_v'] ?? 0), 2); $data['ac0_a'] = round((float)($liveData['ac0_a'] ?? 0), 2);
        $data['ac1_w'] = (int)($liveData['ac1_w'] ?? 0); $data['ac1_v'] = round((float)($liveData['ac1_v'] ?? 0), 2); $data['ac1_a'] = round((float)($liveData['ac1_a'] ?? 0), 2);
        $data['ac2_w'] = (int)($liveData['ac2_w'] ?? 0); $data['ac2_v'] = round((float)($liveData['ac2_v'] ?? 0), 2); $data['ac2_a'] = round((float)($liveData['ac2_a'] ?? 0), 2);

        $gridPhaseValues = [
            liveOptionalFloatValue($liveData['grid_p1'] ?? null, 2),
            liveOptionalFloatValue($liveData['grid_p2'] ?? null, 2),
            liveOptionalFloatValue($liveData['grid_p3'] ?? null, 2),
        ];
        $gridPmFlagRaw = $liveData['grid_pm_available'] ?? ($liveData['Grid_PM_Available'] ?? null);
        $gridPhaseHasSignal = false;
        foreach ($gridPhaseValues as $phaseValue) {
            if ($phaseValue !== null && abs($phaseValue) > 0.1) {
                $gridPhaseHasSignal = true;
                break;
            }
        }
        $gridPmAvailable = ($gridPmFlagRaw === null) ? $gridPhaseHasSignal : liveBoolValue($gridPmFlagRaw, false);
        $data['grid_pm_available'] = $gridPmAvailable;
        $data['grid_pm_index'] = isset($liveData['grid_pm_index']) && is_numeric($liveData['grid_pm_index']) ? (int)$liveData['grid_pm_index'] : null;
        $data['grid_pm_source'] = (string)($liveData['grid_pm_source'] ?? '');
        if ($gridPmAvailable && $gridPhaseValues[0] !== null && $gridPhaseValues[1] !== null && $gridPhaseValues[2] !== null) {
            $data['grid_p1'] = $gridPhaseValues[0];
            $data['grid_p2'] = $gridPhaseValues[1];
            $data['grid_p3'] = $gridPhaseValues[2];
        } else {
            $data['grid_p1'] = null;
            $data['grid_p2'] = null;
            $data['grid_p3'] = null;
        }
        if (($data['wb_source'] ?? '') !== 'openwb_native') {
            $data['wb_p1'] = round((float)($liveData['wb_p1'] ?? 0), 2); $data['wb_p2'] = round((float)($liveData['wb_p2'] ?? 0), 2); $data['wb_p3'] = round((float)($liveData['wb_p3'] ?? 0), 2);
        }
        $pviFrequencyAgeS = $mtime > 0 ? max(0, time() - (int)$mtime) : null;
        $pviFrequency = e3dcLiveFrequencyProjection(
            $liveData['pvi_frequency_hz'] ?? null,
            ($liveData['pvi_frequency_valid'] ?? false) === true,
            (string)($liveData['pvi_frequency_source'] ?? 'unavailable'),
            $pviFrequencyAgeS,
            15.0
        );
        $data['pvi_frequency_hz'] = $pviFrequency['frequency_hz'];
        $data['pvi_frequency_valid'] = $pviFrequency['valid'];
        $data['pvi_frequency_source'] = $pviFrequency['source'];
        $currentFrequency = [
            'frequency_hz' => $data['grid_frequency_hz'],
            'valid' => $data['grid_frequency_valid'] === true,
            'source' => $data['grid_frequency_source'],
            'age_s' => $data['grid_frequency_age_s'],
        ];
        $gridFrequency = e3dcSelectLiveFrequencyProjection(
            $pviFrequency,
            $currentFrequency
        );
        $data['grid_frequency_hz'] = $gridFrequency['frequency_hz'];
        $data['grid_frequency_valid'] = $gridFrequency['valid'];
        $data['grid_frequency_source'] = $gridFrequency['source'];
        $data['grid_frequency_age_s'] = $gridFrequency['age_s'];

        // Der native E3DC-Status ist nur maßgeblich, wenn EXTERN_DATA_ALG frisch
        // und strukturell validiert wurde. Stecker, Verriegelung und Laden sind
        // getrennte Statusbits und dürfen nicht miteinander ODER-verknüpft werden.
        $nativeAlgValid = (($liveData['wb_status_valid'] ?? null) === true);
        if (array_key_exists('wb_status_valid', $liveData)) {
            $directNativeWbStatusInvalid = !$nativeAlgValid;
        }
        $data['wb_status_valid'] = $nativeAlgValid;
        $data['wb_status_source'] = (string)($liveData['wb_status_source'] ?? 'rscp_wb_extern_data_alg');
        $data['wb_status_reason'] = (string)($liveData['wb_status_reason'] ?? 'missing');
        if ($nativeAlgValid) {
            $data['wb_plug'] = (bool)$liveData['wb_plugged'];
            $data['wb_locked'] = (bool)$liveData['wb_locked'];
            $data['wb_charging'] = (bool)$liveData['wb_charging'];
        } elseif (array_key_exists('wb_status_valid', $liveData)) {
            $data['wb_plug'] = null;
            $data['wb_locked'] = null;
            $data['wb_charging'] = null;
        }
        $data['wb_mode'] = (int)($liveData['wb_mode'] ?? ($data['wb_mode'] ?? 0));

        if (isset($liveData['bat_v'])) {
            $data['bat_v'] = round((float)($liveData['bat_v'] ?? 0), 2);
            $data['bat_a'] = round((float)($liveData['bat_a'] ?? 0), 2);
            $data['bat1_v'] = isset($liveData['bat1_v']) ? round((float)$liveData['bat1_v'], 2) : 0;
            $data['bat1_a'] = isset($liveData['bat1_a']) ? round((float)$liveData['bat1_a'], 2) : 0;
        }

        if (isset($liveData['Heizstab_Power'])) {
            $data['hs_power'] = (int)$liveData['Heizstab_Power'];
        }

        // Neue kWh - Retter Stats aus e3dc_live.py mappen
        if (isset($liveData['saved_derating_today_kwh'])) {
            $data['saved_derating_today'] = $liveData['saved_derating_today_kwh'];
            $data['saved_inverter_today'] = $liveData['saved_inverter_today_kwh'];
            $data['alltime_derating']     = $liveData['saved_derating_total_kwh'];
            $data['alltime_inverter']     = $liveData['saved_inverter_total_kwh'];
            $data['alltime_start_date']   = $liveData['retter_start_date'] ?? date('d.m.Y');
        }

        $validData = true;
}

// --- Logik für Wärmepumpen-Verbrauch ---
// Ziel: Den präzisesten verfügbaren Wert verwenden.

// 2. Fallback: Lese Shelly direkt für Wärmepumpe via PHP (zuverlässiger als C++)
// shellyem_ip: alter Config-Key (Luxtronik/IDM Shelly-Zusatzmessung)
// heizstab_shelly_ip: neuer Key für wp_type=2/3 (Shelly Pro3EM)
$shellyWpIp = $confData['config']['heizstab_shelly_ip']
    ?? $confData['config']['shellyem_ip']
    ?? '';
if (!empty($shellyWpIp) && $shellyWpIp !== '0.0.0.0') {
// Nur für wp_type != 3 direkt abfragen: Bei wp_type=3 liefert heizstab_manager
    // den praeziseren Wert aus heizstab_data.json (wurde oben bereits gesetzt).
// Für wp_type=2 und alte shellyem_ip-Konfiguration: Direktabfrage als Fallback.
    if ($wpTypeCfg !== 3 || !isset($data['wp'])) {
        $p = fetchShellyPower($shellyWpIp);
        if ($p !== false) {
            $data['wp'] = $p;
        }
    }
}

// 3. Priorität: Überschreibe mit dem präzisen Wert aus der luxtronik.json ODER waermepumpe.json (IDM)
// WICHTIG: wp_type=3 (Shelly Pro3EM) hat seine eigene Quelle (heizstab_data.json, oben gelesen).
// Die luxtronik/IDM-Logik MUSS uebersprungen werden, sonst ueberschreibt eine alte luxtronik.json
// den bereits korrekt gesetzten $data['wp'] Wert mit 0!
$luxFile = '/var/www/html/ramdisk/luxtronik.json';
$idmFile = '/var/www/html/ramdisk/waermepumpe.json';
$stiebelFile = '/var/www/html/ramdisk/stiebel_isg.json';

// Nimm die Datei, die frischer ist und existiert
$luxTime = file_exists($luxFile) ? filemtime($luxFile) : 0;
$idmTime = file_exists($idmFile) ? filemtime($idmFile) : 0;

$wpSourceFile = '';
$wpSourceJson = null;
$isIdm = false;
$heatManagerSource = null;

if ($wpConfigured && $wpTypeCfg !== 3) {
    if ($wpTypeCfg === 4) {
        // Stiebel darf nur aus einem frischen, erfolgreichen und eindeutig
        // herstellergebundenen Vertrag stammen. Alte Luxtronik-Reste sind
        // für wp_type=4 niemals ein gültiger Fallback.
        $stiebelSelection = e3dcSelectFreshManufacturerPayload(
            [$stiebelFile, $idmFile],
            'Stiebel',
            150
        );
        $data['wp_live_status'] = (string)$stiebelSelection['status'];
        $data['wp_live_fresh'] = ($stiebelSelection['status'] === 'live');
        $data['wp_live_age_s'] = $stiebelSelection['age_s'];
        $data['wp_live_source'] = (string)$stiebelSelection['source'];
        $data['wp_live_error'] = (string)$stiebelSelection['error'];
        if ($stiebelSelection['status'] === 'live') {
            $wpSourceFile = (string)$stiebelSelection['path'];
            $wpSourceJson = $stiebelSelection['payload'];
            $isIdm = false;
        }
    // Dimplex schreibt den normalisierten Vertrag nach waermepumpe.json.
    } elseif ($wpTypeCfg === 5) {
        if ($idmTime > 0) {
            $wpSourceFile = $idmFile;
            $isIdm = false;
        } elseif ($luxTime > 0) {
            $wpSourceFile = $luxFile;
            $isIdm = false;
        }
// Nur für Luxtronik (wp_type=0) und IDM (wp_type=1) - NICHT für Shelly Pro3EM!
    } elseif ($wpTypeCfg === 1) {
        if ($idmTime > 0) {
            $wpSourceFile = $idmFile;
            $isIdm = true;
        } elseif ($luxTime > 0) {
            $wpSourceFile = $luxFile;
            $isIdm = false;
        }
    } else {
        // Luxtronik-Systeme schreiben sowohl waermepumpe.json (WebSocket-Rohdaten)
        // als auch luxtronik.json (normalisierte Manager-Daten). Die Dateizeit darf
        // hier nicht auf IDM umschalten, sonst gehen normalisierte Werte verloren.
        if ($luxTime > 0) {
            $wpSourceFile = $luxFile;
            $isIdm = false;
        } elseif ($idmTime > 0) {
            $wpSourceFile = $idmFile;
            $isIdm = false;
        }
    }
}

$wpSourceIsFresh = $wpSourceFile !== '' && (
    is_array($wpSourceJson)
    || (file_exists($wpSourceFile) && (time() - filemtime($wpSourceFile) < 150))
);
if ($wpSourceIsFresh) {
    $wpJson = is_array($wpSourceJson)
        ? $wpSourceJson
        : @json_decode(file_get_contents($wpSourceFile), true);
    if ($wpJson) {
        $heatManagerSource = $wpJson;
        if ($isIdm) {
            // IDM / Generisches Mapping (waermepumpe.json)
            // Leistungsaufnahme ist in kW -> umrechnen in W
            $pwr = (float)($wpJson['Leistungsaufnahme'] ?? 0) * 1000.0;

            // Fix für IDM Standby-Verbrauch / Messfehler (912W Bug?)
            // Falls Verdichter aus ist, ignorieren wir kleine Lasten unter 1.5 kW
            // oder nutzen den Wert nur, wenn er plausibel ist.
            if (isset($wpJson['Verdichter']) && (int)$wpJson['Verdichter'] === 0) {
                if ($pwr < 1200) $pwr = 0; // Standby-Heizung/Pumpen oft < 1kW, ignorieren für Dashboard
            }

            // Lade ZUSÄTZLICH die Manager-Daten, da waermepumpe.json keine Boost-States enthält
            $luxState = [];
            if (file_exists($luxFile)) {
                $emJson = @json_decode(file_get_contents($luxFile), true);
                if ($emJson) {
                    $luxState = $emJson;
                    $heatManagerSource = $luxState;
                }
            }

            // FAIL-SAFE: Wenn der Shelly (Hardware) nennenswerte Last sieht (> 150W, z.B. Verdichter oder Heizstab),
            // die IDM-Software aber behauptet, sie sei bei 0W, vertrauen wir der Hardware!
            // Liegt der Shelly unter 150W (nur Grundrauschen/Umwälzpumpen), lassen wir die IDM-Software auf saubere 0W glätten!
            $shellyVal = $data['wp'] ?? 0;
            if ($shellyVal > 150 && $pwr == 0) {
                // Behalte den Shelly-Wert!
            } else {
                $data['wp'] = $pwr;
            }

            $data['wp_ww_temp'] = (float)($wpJson['Warmwasser-Ist'] ?? ($wpJson['Warmwasser_Ist'] ?? 0));
            $data['wp_rl_temp'] = (float)($wpJson['Ruecklauf_Ist'] ?? ($wpJson['Rücklauf'] ?? 0));
            $data['wp_vl_temp'] = (float)($wpJson['Vorlauf_Ist'] ?? ($wpJson['Vorlauf'] ?? 0));
            $data['wp_vl_soll'] = (float)($wpJson['Vorlauf_Soll'] ?? ($wpJson['Rückl.-Soll'] ?? 0));
            $kaelteIst = $wpJson['Kaeltespeicher_Ist'] ?? $wpJson['Kaeltespeicher_Temp'] ?? $wpJson['Kältespeicher_Ist'] ?? null;
            if ($kaelteIst !== null && is_numeric($kaelteIst)) {
                $data['wp_kaelte_temp'] = (float)$kaelteIst;
            }
            $kaelteSoll = $wpJson['Kaeltespeicher_Soll'] ?? $wpJson['Kältespeicher_Soll'] ?? null;
            if ($kaelteSoll !== null && is_numeric($kaelteSoll)) {
                $data['wp_kaelte_soll'] = (float)$kaelteSoll;
            }
            // Suche nach Außentemperatur robust:
            $outTemp = 0;
            foreach ($wpJson as $k => $v) {
                if (stripos($k, 'zuluft') !== false || stripos($k, 'Aussentemp') !== false || stripos($k, 'Außentemp') !== false || stripos($k, 'Auentemp') !== false) {
                    $outTemp = (float)$v;
                    break;
                }
            }
            $data['wp_zuluft_temp'] = $outTemp;
            $data['Außentemperatur'] = $outTemp;
            $heatLimit = $confData['config']['heizgrenze_temp'] ?? null;
            if ($heatLimit !== null && is_numeric($heatLimit)) {
                $data['wp_heating_limit_temp'] = (float)$heatLimit;
                $data['wp_season_temp'] = (float)$outTemp;
                $data['wp_season'] = ((float)$outTemp < (float)$heatLimit) ? 'winter' : 'summer';
                $data['wp_season_label'] = ($data['wp_season'] === 'winter') ? 'Winter' : 'Sommer';
            }

            $wpBoostOwner = (string)($luxState['heatpump_boost_owner'] ?? 'none');
            $data['wp_boost_owner'] = $wpBoostOwner;
            $data['wp_boost_active'] = !empty($luxState['boost_active']);
            $data['wp_predump_boost'] = ($wpBoostOwner === 'predump_heatpump') || !empty($luxState['predump_heatpump_active']);
            $data['wp_price_boost'] = !empty($luxState['price_boost_active'])
                && in_array($wpBoostOwner, ['price_plan_heatpump', 'legacy_price_heatpump'], true);
            $data['wp_market_plan'] = !empty($luxState['market_plan_heatpump_active'])
                || $wpBoostOwner === 'market_plan_heatpump';
            if (isset($luxState['market_plan_action'])) $data['market_plan_heatpump_action'] = (string)$luxState['market_plan_action'];
            if (isset($luxState['market_plan_reason'])) $data['market_plan_heatpump_reason'] = (string)$luxState['market_plan_reason'];
            $data['wp_pause_active'] = !empty($luxState['pv_pause_active']);
            $data['wp_manual_boost'] = !empty($luxState['manual_heatpump_active']) || !empty($luxState['manual_ww_boost_active']);
            $data['wp_pre_pause_active'] = !empty($luxState['pre_pause_active']);
            $data['mb_state'] = $luxState['mb_state'] ?? 'IDLE';
            $data['mb_prio'] = $luxState['mb_prio'] ?? '';

            // WICHTIG: Zusätzliche Register für IDM-Anzeige & JAZ-Berechnung (sowie Luxtronik WebSocket)
            if (isset($wpJson['Wärmemenge Gesamt'])) $data['Wärmemenge Gesamt'] = (float)$wpJson['Wärmemenge Gesamt'];
            elseif (isset($wpJson['Wärmemenge_Gesamt'])) $data['Wärmemenge Gesamt'] = (float)$wpJson['Wärmemenge_Gesamt'];
            if (isset($wpJson['Heizleistung Ist']))  $data['Heizleistung Ist'] = (float)$wpJson['Heizleistung Ist'];
            if (isset($wpJson['Leistungsaufnahme'])) $data['Leistungsaufnahme'] = (float)$wpJson['Leistungsaufnahme'];
            if (isset($wpJson['Außentemperatur']))   $data['Außentemperatur'] = (float)$wpJson['Außentemperatur'];
            if (isset($wpJson['Außentemperatur_Mittel'])) $data['Außentemperatur_Mittel'] = (float)$wpJson['Außentemperatur_Mittel'];
            if (isset($wpJson['Verdichter']))      $data['Verdichter'] = (int)$wpJson['Verdichter'];
            if (isset($wpJson['Betriebszustand'])) {
                $bz = $wpJson['Betriebszustand'];
                if (stripos($bz, 'Heiz') !== false)                     $data['wp_mode'] = 0;
                elseif (stripos($bz, 'Warmwasser') !== false)             $data['wp_mode'] = 1;
                elseif (stripos($bz, 'Kühl') !== false)                 $data['wp_mode'] = 2;
                elseif (stripos($bz, 'Abtau') !== false)                $data['wp_mode'] = 4;
                elseif (stripos($bz, 'Aus') !== false || stripos($bz, 'Standby') !== false || stripos($bz, 'steht') !== false) $data['wp_mode'] = 5;
            }
        } else {
            // Luxtronik Mapping (luxtronik.json)
            $wpData = $wpJson['data'] ?? $wpJson;
            $wpVal = function($keys, $default = null) use ($wpData) {
                foreach ($keys as $key) {
                    if (array_key_exists($key, $wpData) && $wpData[$key] !== null && $wpData[$key] !== '' && $wpData[$key] !== '---') {
                        return $wpData[$key];
                    }
                }
                return $default;
            };
            if (isset($wpJson['data']['Leistung_Verdichter_W']) || isset($wpJson['data']['Leistung_Solepumpe_W'])) {
                $comp = $wpJson['data']['Leistung_Verdichter_W'] ?? 0;
                $pump = $wpJson['data']['Leistung_Solepumpe_W'] ?? 0;
                // Fix: Wenn Verdichter aus, dann Verbrauch 0 (verhindert Geisterwerte)
                if (empty($wpJson['data']['Verdichter_Ein'])) {
                    $comp = 0; $pump = 0;
                }

                $luxPwr = $comp + $pump;
                $shellyVal = $data['wp'] ?? 0;

                // FAIL-SAFE: Wenn der Shelly (Hardware) nennenswerte Last sieht (> 150W), aber Luxtronik behauptet,
                // der Verdichter sei aus (0W), vertrauen wir dem Shelly (Heizstab o.ä.).
                if ($shellyVal > 150 && $luxPwr == 0) {
                    // Behalte den Shelly-Wert!
                } else {
                    $data['wp'] = $luxPwr;
                }
            }
            if (isset($wpJson['heatpump_boost_owner'])) $data['wp_boost_owner'] = (string)$wpJson['heatpump_boost_owner'];
            if (isset($wpJson['boost_active'])) $data['wp_boost_active'] = (bool)$wpJson['boost_active'];
            if (isset($wpJson['predump_heatpump_active'])) $data['wp_predump_boost'] = (bool)$wpJson['predump_heatpump_active'];
            if (isset($wpJson['price_boost_active'])) {
                $owner = (string)($data['wp_boost_owner'] ?? 'none');
                $data['wp_price_boost'] = (bool)$wpJson['price_boost_active']
                    && in_array($owner, ['price_plan_heatpump', 'legacy_price_heatpump'], true);
            }
            if (isset($wpJson['market_plan_heatpump_active']) || (string)($data['wp_boost_owner'] ?? 'none') === 'market_plan_heatpump') {
                $data['wp_market_plan'] = !empty($wpJson['market_plan_heatpump_active'])
                    || (string)($data['wp_boost_owner'] ?? 'none') === 'market_plan_heatpump';
            }
            if (isset($wpJson['market_plan_action'])) $data['market_plan_heatpump_action'] = (string)$wpJson['market_plan_action'];
            if (isset($wpJson['market_plan_reason'])) $data['market_plan_heatpump_reason'] = (string)$wpJson['market_plan_reason'];
            if (isset($wpJson['pv_pause_active'])) $data['wp_pause_active'] = (bool)$wpJson['pv_pause_active'];
            if (isset($wpJson['wp_pause_active'])) $data['wp_pause_active'] = (bool)$wpJson['wp_pause_active'];
            if (isset($wpJson['manual_heatpump_active']) || isset($wpJson['manual_ww_boost_active'])) {
                $data['wp_manual_boost'] = !empty($wpJson['manual_heatpump_active']) || !empty($wpJson['manual_ww_boost_active']);
            }
            if (isset($wpJson['pre_pause_active'])) $data['wp_pre_pause_active'] = (bool)$wpJson['pre_pause_active'];
            if (isset($wpJson['idm_ext_ww'])) $data['idm_ext_ww'] = (int)$wpJson['idm_ext_ww'];
            if (isset($wpJson['idm_ext_hz'])) $data['idm_ext_hz'] = (int)$wpJson['idm_ext_hz'];
            if (isset($wpJson['idm_ext_khl'])) $data['idm_ext_khl'] = (int)$wpJson['idm_ext_khl'];
            $luxStatus = (isset($wpJson['status']) && is_array($wpJson['status'])) ? $wpJson['status'] : [];
            $stiebelPowerSource = ($wpTypeCfg === 4) ? $wpVal(['stiebel_power_source', 'Leistungsquelle']) : null;
            $stiebelPowerSourceNorm = strtolower((string)($stiebelPowerSource ?? ''));
            $stiebelPowerSourceAscii = function_exists('iconv') ? @iconv('UTF-8', 'ASCII//TRANSLIT//IGNORE', $stiebelPowerSourceNorm) : false;
            if (is_string($stiebelPowerSourceAscii) && $stiebelPowerSourceAscii !== '') {
                $stiebelPowerSourceNorm = strtolower($stiebelPowerSourceAscii);
            }
            $stiebelPowerSourceIsDhw = ($wpTypeCfg === 4)
                && (strpos($stiebelPowerSourceNorm, 'dhw') !== false || strpos($stiebelPowerSourceNorm, 'warmwasser') !== false);
            $actualLuxState = $wpVal(['Betriebszustand']);
            $actualLuxModeApplied = false;
            if ($actualLuxState !== null && trim((string)$actualLuxState) !== '') {
                $stateNorm = strtolower((string)$actualLuxState);
                $stateAscii = function_exists('iconv') ? @iconv('UTF-8', 'ASCII//TRANSLIT//IGNORE', $stateNorm) : false;
                if (is_string($stateAscii) && $stateAscii !== '') {
                    $stateNorm = strtolower($stateAscii);
                }
                $stateHasWw = (strpos($stateNorm, 'warmw') !== false || preg_match('/(^|[^a-z])ww([^a-z]|$)/', $stateNorm));
                $stateHasCooling = (strpos($stateNorm, 'kuehl') !== false || strpos($stateNorm, 'kuhl') !== false || strpos($stateNorm, 'kühl') !== false || strpos($stateNorm, 'cool') !== false);
                if ($stateHasWw && $stateHasCooling) {
                    $data['wp_mode'] = 2;
                    $data['wp_mode_text'] = 'WW + Kühlen';
                    $actualLuxModeApplied = true;
                } elseif ($stateHasWw) {
                    $data['wp_mode'] = 1;
                    $data['wp_mode_text'] = 'WW';
                    $actualLuxModeApplied = true;
                } elseif ($stateHasCooling) {
                    $data['wp_mode'] = 2;
                    $data['wp_mode_text'] = 'Kühlen';
                    $actualLuxModeApplied = true;
                } elseif (strpos($stateNorm, 'heiz') !== false) {
                    $data['wp_mode'] = 0;
                    $data['wp_mode_text'] = 'Heizen';
                    $actualLuxModeApplied = true;
                } else {
                    $data['wp_mode'] = 5;
                    $data['wp_mode_text'] = 'Standby';
                    $actualLuxModeApplied = true;
                }
            }
            if (!$actualLuxModeApplied) {
                $wwMode = (int)($luxStatus['WW_Mode'] ?? ($data['idm_ext_ww'] ?? 0));
                $hzMode = (int)($luxStatus['HZ_Mode'] ?? ($data['idm_ext_hz'] ?? 0));
                if ($wwMode > 0) {
                    $data['wp_mode'] = 1;
                    $data['wp_mode_text'] = 'WW';
                } elseif ($hzMode > 0) {
                    $data['wp_mode'] = 0;
                    $data['wp_mode_text'] = 'Heizen';
                }
            }
            $wwIst = $wpVal(['Warmwasser_Ist', 'Warmwasser-Ist']);
            $wwSoll = $wpVal(['Warmwasser_Soll', 'Warmwasser-Soll', 'Sollwert Warmw.']);
            $rlIst = $wpVal(['Ruecklauf_Ist', 'Rücklauf', 'Ruecklauf']);
            $rlSoll = $wpVal(['Ruecklauf_Soll', 'Rückl.-Soll', 'Rueckl.-Soll']);
            $vlIst = $wpVal(['Vorlauf_Ist', 'Vorlauf']);
            $vlSoll = $wpVal(['Vorlauf_Soll', 'Mischkreis1 VL-Soll', 'Sollwert Heizen']);
            $hk1Ist = $wpVal(['Heizkreis1_Ist', 'Heizkreis 1 Ist', 'HK1_Ist']);
            $hk1Soll = $wpVal(['Heizkreis1_Soll', 'Heizkreis 1 Soll', 'HK1_Soll']);
            $kaelteIst = $wpVal(['Kaeltespeicher_Ist', 'Kaeltespeicher_Temp', 'Kältespeicher_Ist', 'Puffer_Ist']);
            $kaelteSoll = $wpVal(['Kaeltespeicher_Soll', 'Kältespeicher_Soll']);
            $soleEin = $wpVal(['Sole_Ein', 'Wärmequelle-Ein', 'Waermequelle_Ein']);
            $soleAus = $wpVal(['Sole_Aus', 'Wärmequelle-Aus', 'Waermequelle_Aus']);
            $heatKw = $wpVal(['Leistung_Heiz_kW', 'Heizleistung Ist']);
            $electricW = $wpVal(['Leistung_Verdichter_W']);
            $dimplexHeatEstimated = $wpVal(['dimplex_heat_power_estimated']);
            $dimplexHeatSource = $wpVal(['dimplex_heat_power_source']);
            $dimplexCopEstimate = $wpVal(['dimplex_cop_estimate']);
            if ($electricW === null && $wpVal(['Leistungsaufnahme']) !== null) {
                $electricW = (float)$wpVal(['Leistungsaufnahme']) * 1000.0;
            }
            $energyHeat = $wpVal(['Wärmemenge Gesamt', 'Wärmemenge_Gesamt', 'Energie_Waerme_kWh']);
            $energyElec = $wpVal(['Leistungsaufnahme_Gesamt', 'Energie_Elek_kWh']);
            $stiebelElecDay = ($wpTypeCfg === 4) ? $wpVal(['Strom_Tag_kWh']) : null;
            $stiebelExternalPowerW = ($wpTypeCfg === 4) ? $wpVal(['stiebel_external_power_w', 'external_power_w']) : null;
            $stiebelExternalPowerSource = ($wpTypeCfg === 4) ? $wpVal(['stiebel_external_power_source', 'external_power_source']) : null;
            if ($wpTypeCfg === 5) {
                $dimplexSgValue = $wpVal(['dimplex_sg_value']);
                if ($dimplexSgValue !== null && is_numeric($dimplexSgValue)) {
                    $data['dimplex_sg_value'] = (int)$dimplexSgValue;
                    $data['dimplex_sg_state'] = (string)$wpVal(['dimplex_sg_state'], '');
                    $data['dimplex_sg_color'] = (string)$wpVal(['dimplex_sg_color'], '');
                    $data['wp_source'] = 'dimplex_live';
                    if ((int)$dimplexSgValue === 13) {
                        $data['wp_mode'] = 1;
                        $data['wp_mode_text'] = 'WW';
                    } elseif ((int)$dimplexSgValue === 11) {
                        $data['wp_mode'] = 0;
                        $data['wp_mode_text'] = 'Heizen';
                    } elseif ((int)$dimplexSgValue === 12) {
                        $data['wp_mode'] = 5;
                        $data['wp_mode_text'] = 'EVU-Sperre';
                    } else {
                        $data['wp_mode'] = 5;
                        $data['wp_mode_text'] = 'Normalbetrieb';
                    }
                }
            }
            $compressorOn = !empty($wpData['Verdichter_Ein'])
                || !empty($wpData['stiebel_compressor_running'])
                || ((float)$wpVal(['Verdichter', 'VD-Heizung'], 0) > 0);
            $stiebelCoolingRequested = ($wpTypeCfg === 4) && (
                ((int)$wpVal(['stiebel_cooling_requested', 'Cooling_Request'], 0) > 0)
                || ((int)($luxStatus['Cooling_Request'] ?? 0) > 0)
            );
            $stiebelDhwRequested = ($wpTypeCfg === 4) && (
                ((int)$wpVal(['stiebel_dhw_requested', 'DHW_Request'], 0) > 0)
                || ((int)($luxStatus['DHW_Request'] ?? 0) > 0)
            );
            $stiebelPassiveCooling = $stiebelCoolingRequested && !$compressorOn;
            $wpPowerNow = max(
                (float)($data['wp'] ?? 0),
                (float)($electricW ?? 0),
                ((float)($heatKw ?? 0)) * 1000.0
            );
            if ($wpTypeCfg === 5) {
                $wpPowerNow = max((float)($data['wp'] ?? 0), (float)($electricW ?? 0));
            }
            $coolingActive = ((int)$wpVal(['Kuehlung_Aktiv', 'Kühlung_Aktiv', 'stiebel_cooling_active'], 0) > 0)
                || ((int)$wpVal(['Passive_Kuehlung_Aktiv', 'Passive_Kühlung_Aktiv', 'stiebel_passive_cooling_active'], 0) > 0)
                || $stiebelPassiveCooling
                || (!empty($data['wp_mode_text']) && stripos((string)$data['wp_mode_text'], 'Kühl') !== false);
            if ($stiebelPassiveCooling) {
                $data['wp_mode'] = 2;
                $data['wp_mode_text'] = $stiebelDhwRequested ? 'WW + passive Kühlung' : 'Passive Kühlung';
                $coolingActive = true;
            }
            if ($stiebelPowerSourceIsDhw && !$stiebelPassiveCooling) {
                $data['wp_mode'] = 1;
                $data['wp_mode_text'] = 'WW';
                $coolingActive = false;
            }
            if ($wpTypeCfg === 5 && ($compressorOn || $wpPowerNow >= 150)) {
                $dimplexModeText = (string)$wpVal(['dimplex_operating_mode_text', 'Betriebsmodus'], '');
                $dimplexModeNorm = strtolower($dimplexModeText);
                $dimplexModeAscii = function_exists('iconv') ? @iconv('UTF-8', 'ASCII//TRANSLIT//IGNORE', $dimplexModeNorm) : false;
                if (is_string($dimplexModeAscii) && $dimplexModeAscii !== '') {
                    $dimplexModeNorm = strtolower($dimplexModeAscii);
                }
                $dimplexBlocked = isset($data['dimplex_sg_value']) && (int)$data['dimplex_sg_value'] === 12;
                if (!$dimplexBlocked) {
                    if (strpos($dimplexModeNorm, 'kuehl') !== false || strpos($dimplexModeNorm, 'kuhl') !== false || strpos($dimplexModeNorm, 'cool') !== false) {
                        $data['wp_mode'] = 2;
                        $data['wp_mode_text'] = 'Kühlen';
                    } elseif (strpos($dimplexModeNorm, 'sommer') !== false) {
                        $data['wp_mode'] = 1;
                        $data['wp_mode_text'] = 'WW';
                    } elseif ((int)($data['wp_mode'] ?? 5) === 5) {
                        $data['wp_mode'] = 99;
                        $data['wp_mode_text'] = 'Läuft';
                    }
                }
            }
            if ($wpTypeCfg === 5 && !$compressorOn && $wpPowerNow < 150) {
                $dimplexBlocked = isset($data['dimplex_sg_value']) && (int)$data['dimplex_sg_value'] === 12;
                if (!$dimplexBlocked) {
                    $data['wp_mode'] = 5;
                    $data['wp_mode_text'] = 'Standby';
                    $coolingActive = false;
                }
            }
            if ($wpTypeCfg !== 5 && !$coolingActive && !$compressorOn && $wpPowerNow < 150) {
                $data['wp_mode'] = 5;
                $data['wp_mode_text'] = 'Standby';
            }
            if ($wpTypeCfg === 4 && !$coolingActive && !$compressorOn && (float)($heatKw ?? 0) <= 0.0 && !$stiebelPowerSourceIsDhw) {
                $data['wp_mode'] = 5;
                $data['wp_mode_text'] = 'Standby';
                $coolingActive = false;
            }
            $heatLimit = $wpVal(['Heizgrenze_Temperatur', 'Heizgrenze', 'Heizgrenze_Temp']);
            if ($heatLimit === null || $heatLimit === '') {
                $heatLimit = $confData['config']['heizgrenze_temp'] ?? null;
            }
            $seasonTemp = $wpVal([
                'Aussentemperatur_Mittel', 'Aussentemp_Mittel', 'Gemittelte Außentemperatur',
                'Gemittelte Aussentemperatur', 'Mitteltemperatur',
                'Aussentemperatur', 'Aussentemp', 'Zuluft'
            ]);
            if ($seasonTemp === null) {
                foreach ($wpData as $k => $v) {
                    $lk = strtolower((string)$k);
                    if ((strpos($lk, 'aussen') !== false || strpos($lk, 'zuluft') !== false) && strpos($lk, 'temp') !== false) {
                        $seasonTemp = $v;
                        break;
                    }
                }
            }
            if ($heatLimit !== null && $seasonTemp !== null && is_numeric($heatLimit) && is_numeric($seasonTemp)) {
                $data['wp_heating_limit_temp'] = (float)$heatLimit;
                $data['wp_season_temp'] = (float)$seasonTemp;
                $data['wp_season'] = ((float)$seasonTemp < (float)$heatLimit) ? 'winter' : 'summer';
                $data['wp_season_label'] = ($data['wp_season'] === 'winter') ? 'Winter' : 'Sommer';
            }
            if ($wwIst !== null) $data['wp_ww_temp'] = (float)$wwIst;
            if ($wwSoll !== null) $data['wp_ww_soll'] = (float)$wwSoll;
            if ($rlIst !== null) $data['wp_rl_temp'] = (float)$rlIst;
            if ($rlSoll !== null) $data['wp_rl_soll'] = (float)$rlSoll;
            if ($vlIst !== null) $data['wp_vl_temp'] = (float)$vlIst;
            if ($vlSoll !== null) $data['wp_vl_soll'] = (float)$vlSoll;
            if ($hk1Ist !== null) $data['wp_heating_circuit_temp'] = (float)$hk1Ist;
            if ($hk1Soll !== null) {
                $data['wp_heating_circuit_soll'] = (float)$hk1Soll;
                if ($wpTypeCfg === 4 && $vlSoll === null && $rlSoll === null) {
                    $data['wp_heating_target_temp'] = (float)$hk1Soll;
                }
            }
            if ($kaelteIst !== null && is_numeric($kaelteIst)) $data['wp_kaelte_temp'] = (float)$kaelteIst;
            if ($kaelteSoll !== null && is_numeric($kaelteSoll)) $data['wp_kaelte_soll'] = (float)$kaelteSoll;
            if ($soleEin !== null) { $data['wp_sole_ein_temp'] = (float)$soleEin; $data['Sole_Ein'] = (float)$soleEin; }
            if ($soleAus !== null) { $data['wp_sole_aus_temp'] = (float)$soleAus; $data['Sole_Aus'] = (float)$soleAus; }
            if ($heatKw !== null) { $data['wp_heat_kw'] = (float)$heatKw; $data['Heizleistung Ist'] = (float)$heatKw; }
            if ($wpTypeCfg === 5 && $dimplexHeatEstimated !== null) {
                $data['wp_heat_power_estimated'] = !empty($dimplexHeatEstimated);
                $data['dimplex_heat_power_estimated'] = !empty($dimplexHeatEstimated);
                if ($dimplexHeatSource !== null) $data['dimplex_heat_power_source'] = (string)$dimplexHeatSource;
                if ($dimplexCopEstimate !== null && is_numeric($dimplexCopEstimate)) {
                    $data['dimplex_cop_estimate'] = (float)$dimplexCopEstimate;
                }
            }
            if ($electricW !== null) {
                $data['wp_electric_w'] = (float)$electricW;
                $data['Leistungsaufnahme'] = ((float)$electricW) / 1000.0;
                if ($wpTypeCfg === 4 || !isset($data['wp']) || (float)$data['wp'] <= 0) {
                    $data['wp'] = max(0.0, (float)$electricW);
                }
            }
            if ($stiebelExternalPowerW !== null && is_numeric($stiebelExternalPowerW)) {
                $data['stiebel_external_power_w'] = max(0.0, (float)$stiebelExternalPowerW);
            }
            if ($stiebelExternalPowerSource !== null && trim((string)$stiebelExternalPowerSource) !== '') {
                $data['stiebel_external_power_source'] = (string)$stiebelExternalPowerSource;
            }
            if ($energyHeat !== null) { $data['Wärmemenge Gesamt'] = (float)$energyHeat; $data['Waermemenge_Gesamt'] = (float)$energyHeat; }
            if ($energyElec !== null) { $data['Leistungsaufnahme_Gesamt'] = (float)$energyElec; $data['Energie_Elek_kWh'] = (float)$energyElec; }
            if ($stiebelElecDay !== null && is_numeric($stiebelElecDay)) {
                $stiebelElecDayKwh = max(0.0, (float)$stiebelElecDay);
                $data['Strom_Tag_kWh'] = $stiebelElecDayKwh;
                $data['Energie_Elek_kWh'] = $stiebelElecDayKwh;
                $data['e_wp'] = round($stiebelElecDayKwh, 3);
                $data['e_wp_source'] = 'stiebel_daily_counter';
                $data['wp_daily_counter_kwh'] = round($stiebelElecDayKwh, 3);
            }
            if (isset($wpJson['data']['Warmwasser_Ist'])) $data['wp_ww_temp'] = (float)$wpJson['data']['Warmwasser_Ist'];
            // Fallback für alte Luxtronik-Clients ohne wp_mode
            if (!isset($data['wp_mode']) && isset($wpJson['data']['Betriebsart'])) {
                // Luxtronik Modbus (10002) liefert oft nur den System-Modus (0=Auto, etc).
                // Wir übersteuern das mit dem echten Verdichter-Status:
                if (empty($wpJson['data']['Verdichter_Ein'])) {
                    $data['wp_mode'] = 5;
                    $data['wp_mode_text'] = 'Standby';
                } elseif (!empty($wpJson['wp_boost_active']) && empty($wpJson['wp_pause_active'])) {
                    $data['wp_mode'] = 1;
                    $data['wp_mode_text'] = 'WW';
                } else {
                    $data['wp_mode'] = 0;
                    $data['wp_mode_text'] = 'Heizen';
                }
            }
            if (isset($wpJson['data']['Ruecklauf_Ist'])) $data['wp_rl_temp'] = (float)$wpJson['data']['Ruecklauf_Ist'];

            // Suche robuste Außentemperatur
            if (isset($wpJson['data'])) {
                $outTemp = 0;
                foreach ($wpJson['data'] as $k => $v) {
                    if (stripos($k, 'Aussentemp') !== false || stripos($k, 'Außentemp') !== false || stripos($k, 'Auentemp') !== false) {
                        $outTemp = (float)$v;
                        break;
                    }
                }
                if ($outTemp !== 0) {
                    $data['wp_zuluft_temp'] = $outTemp;
                    $data['Außentemperatur'] = $outTemp;
                }
            }
        }
    }
}

if (is_array($heatManagerSource)) {
    // Die alle zwei Sekunden neu geschriebene Managerdatei beweist allein
    // keinen aktuellen Aktorzustand. Sichtbar wird nur ein explizit
    // zeitgestempelter, bestätigter Relais-/Register-Readback.
    foreach (liveSgReadyReadbackProjection($heatManagerSource) as $key => $value) {
        $data[$key] = $value;
    }
}

if (is_array($heatManagerSource)) {
    if (isset($heatManagerSource['heatpump_budget_w']) && is_numeric($heatManagerSource['heatpump_budget_w'])) {
        $data['heatpump_budget_w'] = (int)round((float)$heatManagerSource['heatpump_budget_w']);
    }
    if (isset($heatManagerSource['storage_state'])) {
        $data['heat_manager_storage_state'] = (string)$heatManagerSource['storage_state'];
    }
    $manualHeatBoost = file_exists('/var/www/html/ramdisk/manual_boost.flag')
        || file_exists('/var/www/html/ramdisk/manual_ww_boost.flag');
    $data['wp_manual_boost'] = $manualHeatBoost;
    $ownerKey = (string)($data['wp_boost_owner'] ?? 'none');
    if (isset($heatManagerSource['market_plan_heatpump_active']) || isset($heatManagerSource['market_plan_action'])) {
        $data['wp_market_plan'] = !empty($heatManagerSource['market_plan_heatpump_active'])
            || $ownerKey === 'market_plan_heatpump';
        if (isset($heatManagerSource['market_plan_action'])) $data['market_plan_heatpump_action'] = (string)$heatManagerSource['market_plan_action'];
        if (isset($heatManagerSource['market_plan_reason'])) $data['market_plan_heatpump_reason'] = (string)$heatManagerSource['market_plan_reason'];
    }
    if (!empty($data['wp_pre_pause_active']) || !empty($data['wp_predump_boost']) || $ownerKey === 'predump_heatpump') {
        $ownerKey = 'predump_heatpump';
    } elseif (!empty($data['wp_market_plan']) || $ownerKey === 'market_plan_heatpump') {
        $ownerKey = 'market_plan_heatpump';
    } elseif (!empty($data['wp_price_boost']) || in_array($ownerKey, ['price_plan_heatpump', 'legacy_price_heatpump'], true)) {
        $ownerKey = ($ownerKey === 'legacy_price_heatpump') ? 'legacy_price_heatpump' : 'price_plan_heatpump';
    } elseif (!empty($data['wp_pause_active']) || in_array($ownerKey, ['legacy_pv_pause', 'source_recovery_heatpump', 'quell_erholung'], true)) {
        $ownerKey = in_array($ownerKey, ['source_recovery_heatpump', 'quell_erholung'], true)
            ? 'source_recovery_heatpump'
            : 'legacy_pv_pause';
    } elseif ($manualHeatBoost) {
        $ownerKey = 'manual_heatpump';
    } elseif (!empty($data['wp_boost_active']) && $ownerKey === 'none') {
        $ownerKey = 'storage_budget_heatpump';
    }
    $ownerInfo = e3dcHeatOwnerInfo($ownerKey);
    $heatActive = !empty($data['wp_boost_active'])
        || !empty($data['wp_price_boost'])
        || !empty($data['wp_market_plan'])
        || !empty($data['wp_predump_boost'])
        || !empty($data['wp_manual_boost'])
        || !empty($data['wp_pause_active'])
        || !empty($data['wp_pre_pause_active'])
        || strtoupper((string)($data['mb_state'] ?? '')) === 'RUNNING';
    $budgetW = isset($data['heatpump_budget_w']) && is_numeric($data['heatpump_budget_w'])
        ? (int)$data['heatpump_budget_w']
        : 0;
    if (strtoupper((string)($data['mb_state'] ?? '')) === 'RUNNING') {
        $heatLabel = 'Morgen-Boost';
        $ownerInfo = [
            'label' => 'Morgen-Boost',
            'reason' => 'Historischer Morgen-Boost läuft; künftig soll dieser Pfad durch Pre-Dump/Storage Manager ersetzt sein.',
            'kind' => 'morning_boost',
        ];
    } elseif (!empty($data['wp_boost_active'])) {
        $heatLabel = $ownerInfo['label'];
    } elseif ($ownerKey !== 'none' && ($ownerInfo['kind'] ?? 'observe') !== 'observe') {
        $heatLabel = $ownerInfo['label'];
    } elseif ($budgetW > 0) {
        $heatLabel = 'Budget bereit';
        $ownerInfo = [
            'label' => 'Budget bereit',
            'reason' => 'Storage Manager bietet Wärmebudget an; die Wärmepumpe nimmt aktuell noch keine Leistung auf.',
            'kind' => 'budget_ready',
        ];
    } else {
        $heatLabel = $ownerInfo['label'];
    }
    $data['heat_manager_active'] = $heatActive;
    $data['heat_manager_label'] = $heatLabel;
    $data['wp_boost_owner'] = $ownerKey;
    $data['heat_manager_owner_key'] = $ownerKey;
    $data['heat_manager_owner_label'] = $ownerInfo['label'];
    $data['heat_manager_owner_kind'] = $ownerInfo['kind'];
    $data['heat_manager_owner_reason'] = $ownerInfo['reason'];
    $reasonParts = [];
    $reasonParts[] = $ownerInfo['reason'];
    if (!empty($data['market_plan_heatpump_action'])) $reasonParts[] = 'Markt ' . $data['market_plan_heatpump_action'];
    if (!empty($data['market_plan_heatpump_reason'])) $reasonParts[] = $data['market_plan_heatpump_reason'];
    if ($budgetW > 0) $reasonParts[] = 'Budget ' . $budgetW . ' W';
    if (!empty($data['wp_mode_text'])) $reasonParts[] = 'WP ' . $data['wp_mode_text'];
    if (!empty($data['heat_manager_storage_state'])) $reasonParts[] = 'Speicher ' . $data['heat_manager_storage_state'];
    $data['heat_manager_reason'] = implode(' | ', $reasonParts);
}

// 4. Externe Wallbox per Shelly oder MQTT (openWB)
if (!$wpConfigured) {
    $data['wp'] = 0;
}
$shellyWbIp = $confData['config']['shelly_wb_ip'] ?? '';
$shellyWb2Ip = $confData['config']['shelly_wb2_ip'] ?? '';
$wbIp = $confData['config']['wb_ip'] ?? '';
$wb2Ip = $confData['config']['wb2_ip'] ?? '';

// $data['wb'] wurde bereits oben aus liveData (Nativ) befüllt, wir initialisieren es hier nur, falls es noch fehlt
if (!isset($data['wb'])) $data['wb'] = 0;
$data['wb2'] = 0;
$data['wb_configured'] = $wbConfigured;
$data['wb2_configured'] = $wb2Configured;
$data['is_external_wb'] = false;
$data['is_external_wb2'] = false;
$configWbType = normalizeWallboxTypeConfig($confData['config']['wb_native_type'] ?? '');
$configWb2Type = normalizeWallboxTypeConfig($confData['config']['wb_native_type2'] ?? '');
$data['wb_native_type'] = $configWbType;
$data['wb2_native_type'] = $configWb2Type;
$nativeWb1StatusContract = ['declared' => false, 'valid' => null, 'source' => '', 'reason' => ''];
$nativeWb2StatusContract = ['declared' => false, 'valid' => null, 'source' => '', 'reason' => ''];
$nativeWb1StatusInvalid = false;
$nativeWb2StatusInvalid = false;
$data['wb_manual_pause'] = in_array(strtolower(trim((string)($confData['config']['wb1_manual_pause'] ?? '0'))), ['1', 'true', 'yes', 'on'], true);
$data['wb2_manual_pause'] = in_array(strtolower(trim((string)($confData['config']['wb2_manual_pause'] ?? '0'))), ['1', 'true', 'yes', 'on'], true);
if ($wbConfigured && $configWbType !== '' && $configWbType !== 'none' && strpos($configWbType, 'e3dc') !== 0) {
    $data['is_external_wb'] = true;
}
if ($wb2Configured && $configWb2Type !== '' && $configWb2Type !== 'none' && strpos($configWb2Type, 'e3dc') !== 0) {
    $data['is_external_wb2'] = true;
}

// Wallbox 1
$data['wp_type'] = (string)$wpTypeCfg;

// --- NEU: Native Wallbox Daten einmischen ---
$wbNativeEnable = isset($confData['config']['wb_native_enable']) && in_array(strtolower(trim($confData['config']['wb_native_enable'])), ['1', 'true']);
$wbNativeFile = '/var/www/html/ramdisk/wallbox_native.json';

if ($wbNativeEnable && ($wbConfigured || $wb2Configured) && file_exists($wbNativeFile)) {
    $nativeWb = @json_decode(file_get_contents($wbNativeFile), true);
    $nativeWbTs = is_array($nativeWb) && is_numeric($nativeWb['ts'] ?? null)
        ? (float)$nativeWb['ts']
        : 0.0;
    $nativeWbAge = (float)time() - $nativeWbTs;
    if ($nativeWb && $nativeWbTs > 0.0 && $nativeWbAge >= -5.0 && $nativeWbAge < 60.0) {
        $nativeTotalPower = (float)($nativeWb['total_power_w'] ?? 0);
        $nativeDetails = (isset($nativeWb['wb_details']) && is_array($nativeWb['wb_details'])) ? $nativeWb['wb_details'] : [];
        $data['wb_details'] = array_values(array_filter(
            $nativeDetails,
            static function($detail) use ($wbConfigured, $wb2Configured) {
                if (!is_array($detail)) return false;
                $detailId = (int)($detail['id'] ?? 0);
                if ($detailId === 1) return $wbConfigured;
                if ($detailId === 2) return $wb2Configured;
                return false;
            }
        ));
        $nativeWb1 = null;
        $nativeWb2 = null;
        foreach ($nativeDetails as $wbDetail) {
            if ($wbConfigured && (int)($wbDetail['id'] ?? 0) === 1) $nativeWb1 = $wbDetail;
            if ($wb2Configured && (int)($wbDetail['id'] ?? 0) === 2) $nativeWb2 = $wbDetail;
        }
        $nativeWbCount = count($data['wb_details']);
        $nativeHasMultiSlots = $nativeWbCount > 1;
        if ($nativeWb1 !== null) {
            $nativeWb1StatusContract = liveNativeWallboxStatusContract($nativeWb1);
            $nativeWb1StatusInvalid = !empty($nativeWb1StatusContract['declared'])
                && (($nativeWb1StatusContract['valid'] ?? null) !== true);
            $data['wb'] = (float)($nativeWb1['power_w'] ?? 0);
            $data['wb_p1'] = (float)($nativeWb1['phase_power_l1_w'] ?? 0);
            $data['wb_p2'] = (float)($nativeWb1['phase_power_l2_w'] ?? 0);
            $data['wb_p3'] = (float)($nativeWb1['phase_power_l3_w'] ?? 0);
            $data['wb_kva'] = liveWallboxApparentKva($nativeWb1);
            $data['wb_power_factor'] = liveWallboxPowerFactor($nativeWb1, $data['wb']);
            $data['wb_set_amp'] = (float)($nativeWb1['current_set_amp'] ?? $nativeWb1['amp'] ?? 0);
            $data['wb_cap_amp'] = (float)($nativeWb1['cap_amp'] ?? $nativeWb1['target_amp'] ?? 0);
            $data['wb_status_amp'] = (float)($nativeWb1['status_amp'] ?? $nativeWb1['amp'] ?? 0);
            liveApplyWallboxFineAmpFields($data, 'wb', $nativeWb1, $data['wb_set_amp']);
            if (!empty($nativeWb1StatusContract['declared'])) {
                $data['wb_status_valid'] = (($nativeWb1StatusContract['valid'] ?? null) === true);
                $data['wb_status_source'] = (string)$nativeWb1StatusContract['source'];
                $data['wb_status_reason'] = (string)$nativeWb1StatusContract['reason'];
            }
            if ($nativeWb1StatusInvalid) {
                $data['wb_plug'] = false;
                $data['wb_locked'] = false;
                $data['wb_charging'] = false;
            } else {
                $data['wb_plug'] = array_key_exists('plug', $nativeWb1) ? (bool)$nativeWb1['plug'] : null;
                $data['wb_locked'] = array_key_exists('plug_locked', $nativeWb1)
                    ? (bool)$nativeWb1['plug_locked']
                    : ($data['wb_locked'] ?? $data['wb_plug']);
                $data['wb_charging'] = array_key_exists('charging', $nativeWb1) ? (bool)$nativeWb1['charging'] : null;
            }
            if (array_key_exists('manual_pause', $nativeWb1)) {
                $data['wb_runtime_manual_pause'] = !empty($nativeWb1['manual_pause']);
            }
            $data['wb_state_text'] = (string)($nativeWb1['state'] ?? '');
            $data['wb_state_reason'] = (string)($nativeWb1['state_reason'] ?? '');
        } elseif ($wbConfigured && !$wb2Configured && $nativeWbCount <= 1 && !$nativeHasMultiSlots) {
            $data['wb'] = $nativeTotalPower;
        } elseif ($wbConfigured) {
            $data['wb'] = 0;
        }
        if ($nativeWb2 !== null) {
            $nativeWb2StatusContract = liveNativeWallboxStatusContract($nativeWb2);
            $nativeWb2StatusInvalid = !empty($nativeWb2StatusContract['declared'])
                && (($nativeWb2StatusContract['valid'] ?? null) !== true);
            $detailWb2Power = (float)($nativeWb2['power_w'] ?? 0);
            if ($detailWb2Power >= 0) {
                $data['wb2'] = $detailWb2Power;
            }
            $data['wb2_p1'] = (float)($nativeWb2['phase_power_l1_w'] ?? 0);
            $data['wb2_p2'] = (float)($nativeWb2['phase_power_l2_w'] ?? 0);
            $data['wb2_p3'] = (float)($nativeWb2['phase_power_l3_w'] ?? 0);
            $data['wb2_kva'] = liveWallboxApparentKva($nativeWb2);
            $data['wb2_power_factor'] = liveWallboxPowerFactor($nativeWb2, (float)($data['wb2'] ?? 0));
            $data['wb2_set_amp'] = (float)($nativeWb2['current_set_amp'] ?? $nativeWb2['amp'] ?? 0);
            $data['wb2_cap_amp'] = (float)($nativeWb2['cap_amp'] ?? $nativeWb2['target_amp'] ?? 0);
            $data['wb2_status_amp'] = (float)($nativeWb2['status_amp'] ?? $nativeWb2['amp'] ?? 0);
            liveApplyWallboxFineAmpFields($data, 'wb2', $nativeWb2, $data['wb2_set_amp']);
            if (!empty($nativeWb2StatusContract['declared'])) {
                $data['wb2_status_valid'] = (($nativeWb2StatusContract['valid'] ?? null) === true);
                $data['wb2_status_source'] = (string)$nativeWb2StatusContract['source'];
                $data['wb2_status_reason'] = (string)$nativeWb2StatusContract['reason'];
            }
            if ($nativeWb2StatusInvalid) {
                $data['wb2_plug'] = false;
                $data['wb2_locked'] = false;
                $data['wb2_charging'] = false;
            } else {
                $data['wb2_plug'] = array_key_exists('plug', $nativeWb2) ? (bool)$nativeWb2['plug'] : null;
                $data['wb2_locked'] = array_key_exists('plug_locked', $nativeWb2)
                    ? (bool)$nativeWb2['plug_locked']
                    : (!empty($nativeWb2['plug']) || (($nativeWb2['state'] ?? '') !== 'Idle'));
                $data['wb2_charging'] = !empty($nativeWb2['charging']) || (($nativeWb2['state'] ?? '') === 'Lade');
            }
            if (array_key_exists('manual_pause', $nativeWb2)) {
                $data['wb2_runtime_manual_pause'] = !empty($nativeWb2['manual_pause']);
            }
            $data['wb2_state_text'] = (string)($nativeWb2['state'] ?? '');
            $data['wb2_state_reason'] = (string)($nativeWb2['state_reason'] ?? '');
        }
        $data['wb_status'] = $nativeWb['status_msg'] ?? '';
        $data['wb_mode_active'] = (int)($nativeWb['wb_mode_active'] ?? ($nativeWb['wb_native_mode'] ?? ($confData['config']['wb_native_mode'] ?? 0)));
        $data['wb_mode'] = $data['wb_mode_active'];
        foreach ([
            'set_amp', 'set_amp_semantics', 'set_max_chargepoint_amp', 'set_power_w',
            'set_phase_amp', 'set_phase_mapping_complete',
            'cap_amp', 'fuzzy_factor', 'fuzzy_delta', 'fuzzy_band_up', 'fuzzy_band_dn', 'ui_hold',
            'avail_wb_w', 'aval_power', 'wb_budget_raw_w', 'wb_budget_curve_w',
            'wb_effective_budget_w', 'wb_effective_extra_w',
            'wb_storage_extra_w', 'storage_charge_reserve_w', 'wb_storage_cap_w',
            'wb_power_for_calc', 'grid_w_raw', 'bat_w_raw', 'soll_soc'
        ] as $nativeKey) {
            if (array_key_exists($nativeKey, $nativeWb)) {
                $data[$nativeKey] = $nativeWb[$nativeKey];
            }
        }
        unset($data['wb_budget_display_contract']);
        if (array_key_exists('wb_budget_display_contract', $nativeWb)) {
            $nativeBudgetDisplayRaw = $nativeWb['wb_budget_display_contract'];
            // Vorhanden, aber null/falsches Schema ist kein Legacy-Payload:
            // Neue Oberflächen müssen dann fail-closed "--" anzeigen.
            $nativeBudgetDisplayContract = e3dcNormalizeWallboxBudgetDisplayContract(
                $nativeBudgetDisplayRaw === null ? [] : $nativeBudgetDisplayRaw
            );
            $data['wb_budget_display_contract'] = $nativeBudgetDisplayContract;
        }
        foreach ([
            'battery_request', 'wbminsoc_gate_open', 'price_opt_active', 'price_boost_active',
            'dynamic_min_soc', 'bat_floor_soc', 'e3dc_wb_discharge_bat_until_soc',
            'wbminsoc_configured_soc', 'wbminsoc_effective_soc', 'wbminsoc_floor_source',
            'wbminsoc_floor_note', 'manual_pause'
        ] as $wbKey) {
            if (array_key_exists($wbKey, $nativeWb)) {
                $data[$wbKey] = $nativeWb[$wbKey];
            }
        }
        foreach (['wb_priority_mode', 'wb_native_distribution_mode', 'wb_priority_label'] as $wbKey) {
            if (array_key_exists($wbKey, $nativeWb)) {
                $data[$wbKey] = $nativeWb[$wbKey];
            }
        }
        if (
            $wbConfigured
            && !$wb2Configured
            && empty($data['is_external_wb'])
            && empty($nativeWb1StatusContract['declared'])
            && $nativeWbCount <= 1
            && !$nativeHasMultiSlots
        ) {
            $nativeTopStatusContract = liveNativeWallboxStatusContract($nativeWb);
            if (!empty($nativeTopStatusContract['declared'])) {
                $nativeWb1StatusContract = $nativeTopStatusContract;
                $nativeWb1StatusInvalid = (($nativeTopStatusContract['valid'] ?? null) !== true);
                $data['wb_status_valid'] = !$nativeWb1StatusInvalid;
                $data['wb_status_source'] = (string)$nativeTopStatusContract['source'];
                $data['wb_status_reason'] = (string)$nativeTopStatusContract['reason'];
                if ($nativeWb1StatusInvalid) {
                    $data['wb_plug'] = false;
                    $data['wb_locked'] = false;
                    $data['wb_charging'] = false;
                }
            }
        }
        $nativeMultiContract = $nativeWb['wb_multi_contract'] ?? null;
        unset($data['wb_multi_contract']);
        if (is_array($nativeMultiContract)) {
            $nativePowerLedger = $nativeMultiContract['power_ledger'] ?? null;
            if (
                is_array($nativePowerLedger)
                && ($nativePowerLedger['schema_version'] ?? null) === 'wallbox_group_power_ledger_v1'
            ) {
                $sanitizedPowerLedger = [
                    'schema_version' => 'wallbox_group_power_ledger_v1',
                ];
                $powerLedgerValid = true;
                foreach ([
                    'gross_group_budget_w',
                    'unmanaged_reserved_w',
                    'managed_budget_w',
                ] as $ledgerKey) {
                    $ledgerValue = $nativePowerLedger[$ledgerKey] ?? null;
                    if (
                        !is_numeric($ledgerValue)
                        || !is_finite((float)$ledgerValue)
                        || (float)$ledgerValue < 0.0
                    ) {
                        $powerLedgerValid = false;
                        break;
                    }
                    $sanitizedPowerLedger[$ledgerKey] = (float)$ledgerValue;
                }
                if ($powerLedgerValid) {
                    $data['wb_multi_contract'] = [
                        'power_ledger' => $sanitizedPowerLedger,
                    ];
                }
            }
        }
        $nativeStatusDetailsDeclared = !empty($nativeWb1StatusContract['declared'])
            || !empty($nativeWb2StatusContract['declared']);
        if ($nativeStatusDetailsDeclared) {
            $nativeConnected = (bool)(
                (!$nativeWb1StatusInvalid && (!empty($data['wb_plug']) || !empty($data['wb_locked'])))
                || (!$nativeWb2StatusInvalid && (!empty($data['wb2_plug']) || !empty($data['wb2_locked'])))
            );
            $nativeCharging = (bool)(
                (!$nativeWb1StatusInvalid && !empty($data['wb_charging']))
                || (!$nativeWb2StatusInvalid && !empty($data['wb2_charging']))
            );
        } else {
            $nativeConnected = (bool)($nativeWb['connected'] ?? false);
            $nativeCharging = (bool)($nativeWb['charging_active'] ?? false);
        }
        $nativePhases = (int)($nativeWb['detected_phases'] ?? 0);
        if ($wbConfigured) {
            $data['connected'] = $nativeConnected;
            $data['charging_active'] = $nativeCharging;
        }
        if ($wbConfigured && !$wb2Configured && $nativeWbCount <= 1 && !$nativeHasMultiSlots) {
            $data['wb_plug'] = $nativeWb1StatusInvalid ? false : $nativeConnected;
            $data['wb_charging'] = $nativeWb1StatusInvalid
                ? false
                : ($nativeCharging || ((float)$data['wb'] > 500));
        }
        if ($wbConfigured && $nativePhases > 0) {
            $data['detected_phases'] = $nativePhases;
            $data['wb_phases'] = $nativePhases;
        }
        // KRITISCH: is_external_wb MUSS auf dem konfigurierten wb_native_type basieren,
        // NICHT auf dem Status-String (wb_type = 'Multi (1 WB)' enthaelt kein 'e3dc'!
        $configWbType = normalizeWallboxTypeConfig($confData['config']['wb_native_type'] ?? '');
        $data['wb_native_type'] = $configWbType;
        $isE3dcMultiConnect = in_array($configWbType, ['e3dc_multi', 'e3dc_multi_connect'], true);
        if ($isE3dcMultiConnect && (float)($data['wb'] ?? 0) > 50) {
            $data['wb_source'] = $data['wb_source'] ?? 'e3dc_multi_native';
            liveResolveE3dcMultiHomeRelation($data, 'wb');
        }
        $configWb2Type = normalizeWallboxTypeConfig($confData['config']['wb_native_type2'] ?? '');
        if ($configWb2Type === ''
            && $wb2Configured
            && e3dcWallbox2RuntimeEvidence($nativeWb)) {
            $configWb2Type = 'openwb';
            $data['is_external_wb2'] = true;
        }
        $data['wb2_native_type'] = $configWb2Type;
        $isE3dcMultiConnect2 = in_array($configWb2Type, ['e3dc_multi', 'e3dc_multi_connect'], true);
        if ($isE3dcMultiConnect2 && (float)($data['wb2'] ?? 0) > 50) {
            $data['wb2_source'] = $data['wb2_source'] ?? 'e3dc_multi_native';
            liveResolveE3dcMultiHomeRelation($data, 'wb2');
        }
        if ($wbConfigured && strpos($configWbType, 'e3dc') !== 0 && $configWbType !== '' && $configWbType !== 'none') {
            $data['is_external_wb'] = true; // Fremd-WB (openWB, go-e, etc.) -> WB aus Home abziehen
        }
        // E3DC Single: false. E3DC Multi Connect: nur bei echter separater WB-Leistung external-like,
        // weil einige Multi-Connect-Zaehlungen sonst in Home_Power stecken bleiben.
        if (isset($nativeWb['wb_details']) && is_array($nativeWb['wb_details'])) {
             // Extrahiere absolute Zählerstände von (Fremd-)Wallboxen, falls Python diese liefert
             foreach ($nativeWb['wb_details'] as $wbDetail) {
                 if ($wbConfigured && $wbDetail['id'] == 1 && isset($wbDetail['total_kwh'])) {
                     $data['python_wb1_total_kwh'] = $wbDetail['total_kwh'];
                     liveSetExactCounter($data, 'e_wb', $wbDetail['total_kwh'], 'wallbox_native_detail', 70);
                 }
	                if ($wbConfigured && $wbDetail['id'] == 1 && isset($wbDetail['session_kwh'])) {
                     $data['python_wb1_session_kwh'] = $wbDetail['session_kwh'];
                 }
                 if ($wb2Configured && $wbDetail['id'] == 2 && isset($wbDetail['total_kwh'])) {
                     $data['python_wb2_total_kwh'] = $wbDetail['total_kwh'];
                     liveSetExactCounter($data, 'e_wb2', $wbDetail['total_kwh'], 'wallbox_native_detail', 70);
                 }
             }
        }
    }
}

if ($wbConfigured && !empty($shellyWbIp) && $shellyWbIp !== '0.0.0.0') {
    $p = fetchShellyPower($shellyWbIp);
    if ($p !== false) {
        $data['wb'] = $p;
        $data['is_external_wb'] = true;
    }
} elseif ($wbConfigured && !empty($wbIp) && $wbIp !== '0.0.0.0') {
    $extWbFile = '/var/www/html/ramdisk/external_wb.json';
    if (file_exists($extWbFile)) {
        $extData = @json_decode(file_get_contents($extWbFile), true);
        if ($extData && e3dcPayloadContextValid($extData)) {
            $wb1Data = $extData['wb1'] ?? $extData; // Fallback
            if (e3dcPayloadContextValid($wb1Data) && isset($wb1Data['ts']) && (time() - $wb1Data['ts'] < 120)) {
                $mqttWbPower = $wb1Data['power'] ?? 0;
                if (is_numeric($mqttWbPower) && is_finite((float)$mqttWbPower)) {
                    $data['wb'] = max(0, (float)$mqttWbPower);
                    $data['is_external_wb'] = true;
                    $data['wb_source'] = $wb1Data['source'] ?? 'external_mqtt';
                    $data['wb_external_topic'] = $wb1Data['topic'] ?? ($confData['config']['wb_topic'] ?? '');
                }
            }
        }
    }
}

// Wallbox 2
$wb2NativeType = strtolower(trim($confData['config']['wb_native_type2'] ?? ''));
$openwbDataFileWb2 = '/var/www/html/ramdisk/openwb_data_wb2.json';
if (($wb2NativeType === 'openwb' || $wb2NativeType === 'openwb_pro')
    && file_exists($openwbDataFileWb2)
    && (time() - filemtime($openwbDataFileWb2) < 30)) {
    $openwbData2 = @json_decode(file_get_contents($openwbDataFileWb2), true);
    if (is_array($openwbData2)) {
        $data['wb2_status_fresh'] = true;
        $owb2Power = (float)($openwbData2['power_w'] ?? 0);
        if ($owb2Power >= 0) {
            $data['wb2'] = $owb2Power;
        }
        $data['wb2_p1'] = (float)($openwbData2['phase_power_l1_w'] ?? 0);
        $data['wb2_p2'] = (float)($openwbData2['phase_power_l2_w'] ?? 0);
        $data['wb2_p3'] = (float)($openwbData2['phase_power_l3_w'] ?? 0);
        $data['wb2_kva'] = liveWallboxApparentKva($openwbData2);
        $data['wb2_power_factor'] = liveWallboxPowerFactor($openwbData2, $owb2Power);
        $data['wb2_set_amp'] = (float)($openwbData2['current_set_amp'] ?? $openwbData2['amp'] ?? ($data['wb2_set_amp'] ?? 0));
        $data['wb2_cap_amp'] = (float)($openwbData2['cap_amp'] ?? $openwbData2['target_amp'] ?? ($data['wb2_cap_amp'] ?? 0));
        $data['wb2_status_amp'] = (float)($openwbData2['status_amp'] ?? $openwbData2['amp'] ?? ($data['wb2_status_amp'] ?? 0));
        liveApplyWallboxFineAmpFields($data, 'wb2', $openwbData2, $data['wb2_set_amp']);
        $data['is_external_wb2'] = true;
        $data['wb2_plug'] = liveBoolValue($openwbData2['plug_state'] ?? false);
        $data['wb2_locked'] = liveBoolValue(
            $openwbData2['locked'] ?? $openwbData2['lock_state'] ?? $openwbData2['plug_locked'] ?? null,
            $data['wb2_plug']
        );
        $data['wb2_session_kwh'] = round((float)($openwbData2['session_kwh'] ?? 0), 2);
        $data['wb2_charging'] = (bool)($openwbData2['charge_state'] ?? false) || $owb2Power > 50;
        $wb2Daily = normalizeOpenwbDailyKwh(
            2,
            (float)($openwbData2['daily_imported_wh'] ?? 0),
            $owb2Power,
            (bool)($openwbData2['charge_state'] ?? false),
            $data['wb2_session_kwh']
        );
        $data['wb2_daily_kwh'] = round((float)$wb2Daily['kwh'], 2);
        $data['wb2_daily_raw_kwh'] = $wb2Daily['raw_kwh'];
        $data['wb2_daily_base_kwh'] = $wb2Daily['baseline_kwh'];
        $data['wb2_chargemode'] = $openwbData2['chargemode'] ?? 'stop';
        $data['wb2_source'] = 'openwb_native';
        $data['wb2_chargepoint_name'] = $openwbData2['chargepoint_name'] ?? '';
        $data['wb2_state_text'] = $openwbData2['state_text'] ?? '';
        $data['wb2_fault_text'] = $openwbData2['fault_text'] ?? '';
        $data['wb2_mode'] = $data['wb2_chargemode'];
        $data['wb2_phases'] = (int)($openwbData2['phases_in_use'] ?? 0);
        $data['wb2_phases_actual'] = (int)($openwbData2['phases_actual'] ?? 0);
        $data['wb2_phases_target'] = (int)($openwbData2['phases_target'] ?? 0);
        $data['wb2_can_switch_phases'] = (bool)($openwbData2['can_switch_phases'] ?? false);
        $data['wb2_phase_switch_capability'] = $openwbData2['phase_switch_capability'] ?? '';
        $data['wb2_phase_switch_source'] = $openwbData2['phase_switch_source'] ?? '';
        $data['wb2_api_surface'] = $openwbData2['api_surface'] ?? '';
        $data['wb2_control_status'] = $openwbData2['control_status'] ?? '';
        $data['wb2_control_label'] = $openwbData2['control_label'] ?? '';
        $data['wb2_control_detail'] = $openwbData2['control_detail'] ?? '';
        $data['wb2_control_level'] = $openwbData2['control_level'] ?? '';
        $data['wb2_last_command_ok'] = $openwbData2['last_command_ok'] ?? null;
        $data['wb2_last_command_amp'] = $openwbData2['last_command_amp'] ?? null;
        $data['wb2_last_heartbeat_ok'] = $openwbData2['last_heartbeat_ok'] ?? null;
        $data['wb2_configured_role'] = $openwbData2['configured_role'] ?? '';
        $data['wb2_detected_role'] = $openwbData2['detected_role'] ?? '';
        $data['wb2_effective_role'] = $openwbData2['effective_role'] ?? '';
        $data['wb2_role_mismatch'] = (bool)($openwbData2['role_mismatch'] ?? false);
        $data['wb2_command_failure_count'] = (int)($openwbData2['command_failure_count'] ?? 0);
        $data['wb2_command_failure_limit'] = (int)($openwbData2['command_failure_limit'] ?? 3);
        $data['wb2_command_blocked'] = (bool)($openwbData2['command_blocked'] ?? false);
        $data['wb2_evse_a'] = round((float)($openwbData2['evse_current'] ?? 0), 1);
        $data['wb2_cp_id'] = $openwbData2['cp_id'] ?? 'pro';
        $data['wb2_native_ip'] = $confData['config']['wb_native_ip2'] ?? '';
        $wb2SocSource = (string)($openwbData2['car_soc_source'] ?? '');
        $wb2SocSourceTs = wallboxSocSourceTimestamp(
            $openwbData2,
            $wb2SocSource
        );
        $wb2SocValue = vehicleSocPercentValue($openwbData2['car_soc'] ?? null);
        $wb2SocConfirmed = $wb2SocValue !== null
            && !wallboxSocPayloadExplicitVetoed($openwbData2)
            && wallboxSocTruthConfirmed(
            $wb2SocSource,
            $openwbData2['car_soc_rule_confirmed'] ?? null,
            $wb2SocSourceTs,
            null,
            array_key_exists('car_soc_rule_confirmed', $openwbData2),
            $openwbData2
        );
        $data['wb2_soc'] = $wb2SocConfirmed ? $wb2SocValue : 0.0;
        $data['wb2_soc_source'] = $wb2SocSource;
        $data['wb2_soc_source_ts'] = $wb2SocSourceTs;
        $data['wb2_soc_rule_confirmed'] = $wb2SocConfirmed;
        $data['wb2_soc_age_contract'] = $openwbData2['car_soc_age_contract'] ?? null;
        $data['wb2_soc_age_contract_source'] = $openwbData2['car_soc_age_contract_source'] ?? null;
        $data['wb2_soc_max_age_s'] = $openwbData2['car_soc_max_age_s'] ?? null;
        $wb2RangeContract = openwbTotalRangeContract($openwbData2);
        $data['wb2_range'] = is_array($wb2RangeContract)
            ? (float)$wb2RangeContract['range_km']
            : 0.0;
        foreach ([
            'car_range_source' => 'wb2_range_source',
            'car_range_valid' => 'wb2_range_valid',
            'car_range_observed_ts' => 'wb2_range_observed_ts',
            'car_range_source_ts' => 'wb2_range_source_ts',
            'car_range_source_ts_explicit' => 'wb2_range_source_ts_explicit',
            'car_range_vehicle_key' => 'wb2_range_vehicle_key',
        ] as $contractKey => $dataKey) {
            $data[$dataKey] = is_array($wb2RangeContract)
                ? $wb2RangeContract[$contractKey]
                : null;
        }
        $data['wb2_charged_range'] = ($wb2SocConfirmed && $wb2SocValue > 0) ? (float)($openwbData2['car_charged_range'] ?? $openwbData2['range_charged'] ?? 0) : 0.0;
        $data['wb2_charge_profile_name'] = trim((string)($openwbData2['charge_template_name'] ?? ''));
        $data['wb2_charge_profile_source'] = $data['wb2_charge_profile_name'] !== '' ? 'openwb_charge_template' : '';
        $wb2IdentityCurrent = array_key_exists('stable_vehicle_identity_current', $openwbData2)
            ? (bool)$openwbData2['stable_vehicle_identity_current']
            : false;
        $wb2HasCurrentVehicle = !empty($data['wb2_plug']) || !empty($data['wb2_charging']) || $owb2Power > 50;
        if ($wb2HasCurrentVehicle) {
            $data['wb2_car_name'] = trim((string)($openwbData2['car_name'] ?? ''));
            $data['wb2_car_id'] = $openwbData2['car_id'] ?? null;
            $data['wb2_vehicle_id'] = $openwbData2['vehicle_id'] ?? null;
            $data['wb2_rfid_tag'] = $openwbData2['rfid_tag'] ?? null;
            $data['wb2_rfid_timestamp'] = $openwbData2['rfid_timestamp'] ?? null;
            $data['wb2_live_car_name'] = $data['wb2_car_name'];
            $data['wb2_live_car_id'] = $data['wb2_car_id'];
            $data['wb2_live_vehicle_id'] = $data['wb2_vehicle_id'];
            $data['wb2_live_rfid_tag'] = $data['wb2_rfid_tag'];
            $data['wb2_vehicle_identity_current'] = $wb2IdentityCurrent;
            $data['wb2_stable_vehicle_identity_current'] = $wb2IdentityCurrent;
            $wb2DisplayVehicle = resolveWallboxDisplayVehicle(
                true,
                $wb2IdentityCurrent,
                $data['wb2_car_name'],
                $data['wb2_charge_profile_name']
            );
            $data['wb2_display_car_name'] = $wb2DisplayVehicle['name'];
            $data['wb2_display_car_source'] = $wb2DisplayVehicle['source'];
        } else {
            $data['wb2_car_name'] = '';
            $data['wb2_car_id'] = null;
            $data['wb2_vehicle_id'] = null;
            $data['wb2_rfid_tag'] = null;
            $data['wb2_rfid_timestamp'] = null;
            $data['wb2_live_car_name'] = '';
            $data['wb2_live_car_id'] = null;
            $data['wb2_live_vehicle_id'] = null;
            $data['wb2_live_rfid_tag'] = null;
            $data['wb2_vehicle_identity_current'] = false;
            $data['wb2_stable_vehicle_identity_current'] = false;
            $data['wb2_display_car_name'] = '';
            $data['wb2_display_car_source'] = '';
        }
        $data['wb2_pro_serial'] = $openwbData2['serial'] ?? null;
        $data['wb2_pro_temp_c'] = $openwbData2['temp_c'] ?? null;
        liveSetExactCounter($data, 'e_wb2', $data['wb2_daily_kwh'], $wb2Daily['source'], 90);
    }
}
if ($wb2Configured && !empty($shellyWb2Ip) && $shellyWb2Ip !== '0.0.0.0') {
    $p = fetchShellyPower($shellyWb2Ip);
    if ($p !== false) {
        $data['wb2'] = $p;
        $data['is_external_wb2'] = true;
    }
} elseif ($wb2Configured && !empty($wb2Ip) && $wb2Ip !== '0.0.0.0') {
    $extWbFile = '/var/www/html/ramdisk/external_wb.json';
    if (file_exists($extWbFile)) {
        $extData = @json_decode(file_get_contents($extWbFile), true);
        if ($extData && e3dcPayloadContextValid($extData) && isset($extData['wb2'])) {
            $wb2Data = $extData['wb2'];
            if (e3dcPayloadContextValid($wb2Data) && isset($wb2Data['ts']) && (time() - $wb2Data['ts'] < 120)) {
                $mqttWb2Power = $wb2Data['power'] ?? 0;
                if (is_numeric($mqttWb2Power) && is_finite((float)$mqttWb2Power)) {
                    $data['wb2'] = max(0, (float)$mqttWb2Power);
                    $data['is_external_wb2'] = true;
                    $data['wb2_source'] = $wb2Data['source'] ?? 'external_mqtt';
                    $data['wb2_external_topic'] = $wb2Data['topic'] ?? ($confData['config']['wb2_topic'] ?? '');
                }
            }
        }
    }
}

$wb1UsesExternalStatus = !empty($data['is_external_wb']);

// WB2-Anzeige-Hysterese: openWB/openWB Pro liefert gelegentlich kurze 0W- oder
// stale-Luecken. Ohne Hold springt der bereinigte Hausverbrauch um die WB-Leistung.
if ($wb2Configured) {
$wb2DisplayCacheFile = '/var/www/html/ramdisk/wb2_display_power_cache.json';
$wb2DisplayCache = file_exists($wb2DisplayCacheFile)
    ? (@json_decode(file_get_contents($wb2DisplayCacheFile), true) ?: [])
    : [];
$wb2DisplayNow = abs((float)($data['wb2'] ?? 0));
$wb2PhaseDisplaySum = abs((float)($data['wb2_p1'] ?? 0)) + abs((float)($data['wb2_p2'] ?? 0)) + abs((float)($data['wb2_p3'] ?? 0));
$wb2FreshZeroStopped = !empty($data['wb2_status_fresh'])
    && empty($data['wb2_charging'])
    && (($data['wb2_evse_a'] ?? 0) < 5.5)
    && $wb2DisplayNow <= 50
    && $wb2PhaseDisplaySum <= 50;
$wb2LooksActive = !$wb2FreshZeroStopped && (
    !empty($data['wb2_locked'])
    || !empty($data['wb2_plug'])
    || !empty($data['wb2_charging'])
    || (($data['wb2_evse_a'] ?? 0) >= 5.5)
    || ($wb2PhaseDisplaySum > 50)
    || (!empty($wb2DisplayCache['active']) && (time() - ($wb2DisplayCache['ts'] ?? 0)) < 45)
);
if ($wb2DisplayNow <= 50 && $wb2PhaseDisplaySum > 50) {
    $data['wb2'] = $wb2PhaseDisplaySum;
    $data['wb2_display_held'] = true;
    $data['is_external_wb2'] = true;
    $wb2DisplayNow = $wb2PhaseDisplaySum;
}
if ($wb2DisplayNow > 50) {
    $wb2DisplayCache = [
        'power_w' => $wb2DisplayNow,
        'ts' => time(),
        'active' => $wb2LooksActive,
        'locked' => !empty($data['wb2_locked']),
        'plug' => !empty($data['wb2_plug']),
        'external' => !empty($data['is_external_wb2']),
    ];
    @file_put_contents($wb2DisplayCacheFile, json_encode($wb2DisplayCache), LOCK_EX);
    @chmod($wb2DisplayCacheFile, 0664);
} elseif ($wb2FreshZeroStopped) {
    if (!empty($wb2DisplayCache['power_w'])) {
        @unlink($wb2DisplayCacheFile);
    }
} elseif ($wb2LooksActive
    && !empty($wb2DisplayCache['power_w'])
    && (time() - ($wb2DisplayCache['ts'] ?? 0)) < 45) {
    $data['wb2'] = (float)$wb2DisplayCache['power_w'];
    $data['wb2_display_held'] = true;
    if (!empty($wb2DisplayCache['external'])) {
        $data['is_external_wb2'] = true;
    }
    if (!empty($wb2DisplayCache['locked']) || !empty($wb2DisplayCache['plug'])) {
        $data['wb2_locked'] = true;
        $data['wb2_plug'] = true;
    }
}
}

// Anzeige-Hysterese für alle WB-Quellen: Beim Ampere-Setzen liefern E3DC/openWB
// kurz 0W oder gar keinen Messwert. Wichtig: Dieser Hold muss NACH allen WB-Quellen
// sitzen, sonst kann wallbox_native.json den gehaltenen Wert wieder mit 0W ueberschreiben.
if ($wbConfigured) {
$wbDisplayCacheFile = '/var/www/html/ramdisk/wb_display_power_cache.json';
$wbDisplayCache = file_exists($wbDisplayCacheFile)
    ? (@json_decode(file_get_contents($wbDisplayCacheFile), true) ?: [])
    : [];
$wbDisplayNow = abs((float)($data['wb'] ?? 0));
$wbPhaseDisplaySum = abs((float)($data['wb_p1'] ?? 0)) + abs((float)($data['wb_p2'] ?? 0)) + abs((float)($data['wb_p3'] ?? 0));
$wbFreshZeroStopped = !empty($data['wb_status_fresh'])
    && empty($data['wb_charging'])
    && (($data['wb_evse_a'] ?? 0) < 5.5)
    && $wbDisplayNow <= 50
    && $wbPhaseDisplaySum <= 50;
$wbLooksActive = !$wbFreshZeroStopped && (!empty($data['wb_locked'])
    || !empty($data['wb_plug'])
    || !empty($data['wb_charging'])
    || (($data['wb_evse_a'] ?? 0) >= 5.5)
    || ($wbPhaseDisplaySum > 50)
    || ($wbDisplayNow > 50)
    || (!empty($data['is_external_wb']) && !empty($wbDisplayCache['active']) && (time() - ($wbDisplayCache['ts'] ?? 0)) < 45));
$wbAmpLimit = max((float)($data['set_amp'] ?? 0), (float)($data['cap_amp'] ?? 0));
$wbPhaseLimit = max(1, (int)($data['wb_phases'] ?? ($data['detected_phases'] ?? 1)));
$wbExpectedW = $wbAmpLimit * 230.0 * $wbPhaseLimit;
if ($wbDisplayNow <= 50 && $wbPhaseDisplaySum > 50) {
    $data['wb'] = $wbPhaseDisplaySum;
    $data['wb_display_held'] = true;
    $wbDisplayNow = $wbPhaseDisplaySum;
}
$wbImplausibleMax = $wbDisplayNow > 1000
    && (($wbExpectedW > 0 && $wbDisplayNow > $wbExpectedW * 1.45) || ($wbExpectedW <= 0 && $wbDisplayNow > 18000));
if ($wbImplausibleMax
    && !empty($wbDisplayCache['power_w'])
    && (time() - ($wbDisplayCache['ts'] ?? 0)) < 45) {
    $data['wb'] = (float)$wbDisplayCache['power_w'];
    $data['wb_display_held'] = true;
    if (!empty($wbDisplayCache['locked']) || !empty($wbDisplayCache['plug'])) {
        $data['wb_locked'] = true;
        $data['wb_plug'] = true;
    }
    $wbDisplayNow = abs((float)$data['wb']);
}
if ($wbDisplayNow > 50) {
    $wbDisplayCache = [
        'power_w' => $wbDisplayNow,
        'ts' => time(),
        'active' => $wbLooksActive,
        'locked' => !empty($data['wb_locked']),
        'plug' => !empty($data['wb_plug']),
        'external' => !empty($data['is_external_wb']),
        'source' => $data['wb_source'] ?? '',
    ];
    @file_put_contents($wbDisplayCacheFile, json_encode($wbDisplayCache), LOCK_EX);
    @chmod($wbDisplayCacheFile, 0664);
} elseif ($wbFreshZeroStopped) {
    if (!empty($wbDisplayCache['power_w'])) {
        @unlink($wbDisplayCacheFile);
    }
} elseif ($wbLooksActive
    && !empty($wbDisplayCache['power_w'])
    && (time() - ($wbDisplayCache['ts'] ?? 0)) < 45) {
    $data['wb'] = (float)$wbDisplayCache['power_w'];
    $data['wb_display_held'] = true;
    if (!empty($wbDisplayCache['locked']) || !empty($wbDisplayCache['plug'])) {
        $data['wb_locked'] = true;
        $data['wb_plug'] = true;
    }
    if (!empty($wbDisplayCache['external'])) {
        $data['is_external_wb'] = true;
        if (empty($data['wb_source'])) $data['wb_source'] = $wbDisplayCache['source'] ?? 'external_mqtt';
    }
}
}

e3dcApplyWallboxPresenceProjection($data, $wbConfigured, $wb2Configured, $wb2ExplicitlyDisabled);

// Native E3DC-Wallbox: Phasen werden nicht von openWB geliefert, sondern aus den
// echten L1/L2/L3-Leistungen abgeleitet. Das ueberschreibt auch alte/stale UI-Werte.
$wbPhaseCount = 0;
foreach (['wb_p1', 'wb_p2', 'wb_p3'] as $phaseKey) {
    if (abs((float)($data[$phaseKey] ?? 0)) > 250) {
        $wbPhaseCount++;
    }
}
if ($wbPhaseCount > 0 && (abs((float)($data['wb'] ?? 0)) > 500 || !empty($data['wb_locked']))) {
    $data['detected_phases'] = $wbPhaseCount;
    $data['wb_phases'] = $wbPhaseCount;
}
if (empty($data['wb_phases']) && !empty($data['detected_phases'])) {
    $data['wb_phases'] = (int)$data['detected_phases'];
}
$activeWbPhases = 0;
$activeWbId = 0;
$wb2PhasePowerSum = abs((float)($data['wb2_p1'] ?? 0)) + abs((float)($data['wb2_p2'] ?? 0)) + abs((float)($data['wb2_p3'] ?? 0));
$wb2PhaseDisplayActive = !empty($data['wb2_charging'])
    && (abs((float)($data['wb2'] ?? 0)) > 50 || $wb2PhasePowerSum > 250);
$wbPhaseDisplayActive = !empty($data['wb_charging'])
    && (abs((float)($data['wb'] ?? 0)) > 50 || $wbPhaseDisplaySum > 250);
if ($wb2PhaseDisplayActive) {
    $activeWbId = 2;
    $activeWbPhases = (int)($data['wb2_phases'] ?? 0);
    if ($activeWbPhases <= 0) {
        foreach (['wb2_p1', 'wb2_p2', 'wb2_p3'] as $phaseKey) {
            if (abs((float)($data[$phaseKey] ?? 0)) > 250) {
                $activeWbPhases++;
            }
        }
    }
    if ($activeWbPhases <= 0) {
        $activeWbPhases = (int)($data['wb2_phases_actual'] ?? $data['wb2_phases_target'] ?? 0);
    }
} elseif ($wbPhaseDisplayActive) {
    $activeWbId = 1;
    $activeWbPhases = (int)($data['wb_phases'] ?? $data['detected_phases'] ?? 0);
    if ($activeWbPhases <= 0) {
        foreach (['wb_p1', 'wb_p2', 'wb_p3'] as $phaseKey) {
            if (abs((float)($data[$phaseKey] ?? 0)) > 250) {
                $activeWbPhases++;
            }
        }
    }
}
if ($activeWbPhases > 0) {
    $data['active_wb_id'] = $activeWbId;
    $data['active_wb_phases'] = max(1, min(3, $activeWbPhases));
}
if (abs((float)($data['wb'] ?? 0)) > 500 && !empty($data['wb_plug']) && ($data['wb_status_valid'] ?? false)) {
    $data['wb_charging'] = true;
    $data['charging_active'] = true;
}

// Externe Legacy-Treiber stellen eventuell kein Verriegelungsbit bereit. Für
// validierte native E3DC-Telemetrie wird es nie synthetisiert: Eingesteckt,
// verriegelt und ladend bleiben getrennt.
if (empty($data['wb_status_valid']) && (!isset($data['wb_locked']) || $data['is_external_wb'])) {
    $data['wb_locked'] = (abs($data['wb']) > 50);
}
if (isset($data['wb2_plug']) || isset($data['wb2_charging'])) {
    $data['wb2_locked'] = !empty($data['wb2_plug']) || !empty($data['wb2_charging']) || abs($data['wb2']) > 50;
} elseif (!isset($data['wb2_locked']) || $data['wb2_locked'] === null || $data['is_external_wb2']) {
    $data['wb2_locked'] = (abs($data['wb2']) > 50);
}

// Ein frisches wallbox_native.json belegt nur die Dateifrische. Sobald der
// darin deklarierte E3DC-Statusvertrag ungültig ist, dürfen Anzeige-Holds und
// Leistungsfallbacks keine alte Stecker-/Verriegelungskante wiederbeleben.
$nativeWb1StatusInvalid = $nativeWb1StatusInvalid || (
    $wbConfigured
    && !$wb1UsesExternalStatus
    && empty($nativeWb1StatusContract['declared'])
    && $directNativeWbStatusInvalid
);
if ($nativeWb1StatusInvalid) {
    $data['wb_plug'] = false;
    $data['wb_locked'] = false;
    $data['wb_charging'] = false;
    if ((int)($data['active_wb_id'] ?? 0) === 1) {
        unset($data['active_wb_id'], $data['active_wb_phases']);
    }
}
if ($nativeWb2StatusInvalid) {
    $data['wb2_plug'] = false;
    $data['wb2_locked'] = false;
    $data['wb2_charging'] = false;
    if ((int)($data['active_wb_id'] ?? 0) === 2) {
        unset($data['active_wb_id'], $data['active_wb_phases']);
    }
}
if ($nativeWb1StatusInvalid || $nativeWb2StatusInvalid) {
    $data['connected'] = (bool)(
        ($wbConfigured && !$nativeWb1StatusInvalid && (!empty($data['wb_plug']) || !empty($data['wb_locked'])))
        || ($wb2Configured && !$nativeWb2StatusInvalid && (!empty($data['wb2_plug']) || !empty($data['wb2_locked'])))
    );
    $data['charging_active'] = (bool)(
        ($wbConfigured && !$nativeWb1StatusInvalid && !empty($data['wb_charging']))
        || ($wb2Configured && !$nativeWb2StatusInvalid && !empty($data['wb2_charging']))
    );
}

// --- FAIL-SAFE: Verbrauchsanteile (WB/WP) aus dem Hausverbrauch abziehen ---
// E3DC rechnet oft alle internen Lasten in den "Hausverbrauch" ein.
// Wir berechnen hier den "sauberen" Hausverbrauch (Licht, Kochen, Steckdosen).
if (!isset($data['home'])) {
    $data['home'] = (float)$data['home_raw'];
}

// 1. Wärmepumpe abziehen (falls sie Teil des Hausverbrauchs ist)
if ($data['wp'] > 0) {
    $data['home'] = max(0, $data['home'] - $data['wp']);
}

// 2. Wallboxen abziehen
// NUR bei Fremd-Wallboxen und E3DC Multi Connect mit echter separater WB-Leistung:
// Home_Power enthaelt dort je nach Quelle die WB-Last.
// Bei nativer E3DC-Single-Wallbox: RSCP liefert Home_Power normalerweise OHNE WB-Anteil.
if ($data['wb'] > 0 && !empty($data['is_external_wb'])) {
    $data['home'] = max(0, $data['home'] - $data['wb']);
}
if ($data['wb2'] > 0 && !empty($data['is_external_wb2'])) {
    $data['home'] = max(0, $data['home'] - $data['wb2']);
}
if (isset($data['hs_power']) && $data['hs_power'] > 0) {
    $data['home'] = max(0, $data['home'] - $data['hs_power']);
}
if (isset($data['climate_power_w']) && $data['climate_power_w'] > 0) {
    $data['home'] = max(0, $data['home'] - $data['climate_power_w']);
}

// --- SICHERHEITS-FILTER FÜR C++ INITIALISIERUNGS-BUG ---
// Beim Neustart des C++ Programms ist fstrompreis kurzzeitig 10000.
// Wir filtern unplausible Werte (> 500 ct/kWh) rigoros heraus.
if ($currentPrice !== null && $currentPrice > 500) {
    $currentPrice = null;
}
// Fallback auf den vorberechneten Fahrplan aus der awattardebug.txt
if (isset($scheduledPrice) && $scheduledPrice !== null) {
    $currentPrice = $scheduledPrice;
    $data['price_source'] = 'schedule_fallback';
}

if ($currentPrice !== null) {
    $data['price_ct'] = round($currentPrice, 2);
    $data['price_level'] = classifyPriceLevel($currentPrice, $minPrice, $maxPrice);
}
else {
    $data['price_ct'] = null;
    $data['price_level'] = 'unknown';
}

// --- HA Status vorab laden, um Standby zu erkennen ---
$isStandby = false;
$haFile = '/var/www/html/ramdisk/ha_status.json';
if (file_exists($haFile)) {
    $haData = @json_decode(file_get_contents($haFile), true);
    if ($haData) {
        if (time() - ($haData['ts'] ?? 0) > 180) {
            $haData['state'] = 'offline';
        }
        $data['ha'] = $haData;
        $isStandby = ($haData['mode'] === 'slave' && $haData['state'] !== 'failover');
    }
}

// --- NEU: Sekundengenaues Wallbox Session Tracking (Echtzeit-Integrator) ---
$wbStateFile = '/var/www/html/ramdisk/wb_live_session.json';
$wb2StateFile = '/var/www/html/ramdisk/wb2_live_session.json';
$wbCsvFile = '/var/www/html/data/wb_sessions.csv';
$wbSessionKwh = 0;
$wb2SessionKwh = 0;

// C++ Nativer Session- / Tages-Zähler (Priorität vor PHP-Echtzeit-Integrator)
$nativeWbSession = isset($liveData) ? ($liveData['Wallbox_Energy_kWh'] ?? $liveData['Wb_Energy_kWh'] ?? $liveData['WB_Energy_kWh'] ?? $liveData['wb_session_kwh'] ?? null) : null;
// NEU: Verwende priorisiert den direkten Wallbox Total-Zähler, falls Python uns diesen für eine Fremd-Wallbox holt!
if (isset($data['python_wb1_total_kwh'])) {
    $nativeWbSession = $data['python_wb1_total_kwh'];
} elseif (isset($data['python_wb1_session_kwh'])) {
    $nativeWbSession = $data['python_wb1_session_kwh'];
} else if (!empty($data['is_external_wb'])) {
    $nativeWbSession = null;
}

// Vor Session-Integratoren und Statistikschreibpfaden gilt bereits derselbe
// fail-closed Präsenzvertrag wie am JSON-Ausgang. Ein deaktivierter Slot darf
// weder aus Legacy-/Sessionwerten wiederauferstehen noch Zähler fortschreiben.
e3dcApplyWallboxPresenceProjection($data, $wbConfigured, $wb2Configured, $wb2ExplicitlyDisabled);

if ($validData && !$isStandby) {
    $nowTs = time();
    $currentWbPower = (float)$data['wb'];
    $currentWb2Power = (float)$data['wb2'];
    $isLocked = (bool)($data['wb_locked'] ?? false);
    $isLocked2 = (bool)($data['wb2_locked'] ?? false);

    // --- Wallbox 1 Tracking ---
    if ($wbConfigured) {
    $wbLockFile = '/var/www/html/tmp/wb_session.lock';
    if (!is_dir(dirname($wbLockFile))) { @mkdir(dirname($wbLockFile), 0775, true); }
    $wbFp = @fopen($wbLockFile, 'c+');

    if ($wbFp && @flock($wbFp, LOCK_EX | LOCK_NB)) {
        $todayKey = date('Y-m-d', $nowTs);
        $wbState = ['is_locked' => false, 'start_ts' => '', 'last_ts' => $nowTs, 'kwh' => 0, 'daily_date' => $todayKey, 'daily_kwh' => 0, 'daily_last_ts' => $nowTs];
        $hadDailyKwh = false;
        if (file_exists($wbStateFile)) {
            $parsedState = @json_decode(file_get_contents($wbStateFile), true);
            if (is_array($parsedState)) {
                $hadDailyKwh = array_key_exists('daily_kwh', $parsedState);
                $wbState = array_merge($wbState, $parsedState);
            }
        }
        if (($wbState['daily_date'] ?? '') !== $todayKey) {
            $wbState['daily_date'] = $todayKey;
            $wbState['daily_kwh'] = 0.0;
            $wbState['daily_last_ts'] = $nowTs;
        } elseif (!$hadDailyKwh && !empty($wbState['is_locked']) && strpos((string)($wbState['start_ts'] ?? ''), $todayKey) === 0) {
            $wbState['daily_kwh'] = max(0.0, (float)($wbState['kwh'] ?? 0));
            $wbState['daily_last_ts'] = $nowTs;
        }

        if ($isLocked) {
            // Stecker angesteckt: Grace-Zaehler loeschen
            $wbState['unlock_ts'] = 0;
            if (!$wbState['is_locked']) {
                $wbState['is_locked'] = true;
                $wbState['start_ts'] = date('Y-m-d H:i:s', $nowTs);
                $wbState['last_ts'] = $nowTs;
                $wbState['start_kwh'] = $nativeWbSession !== null ? (float)$nativeWbSession : 0;
                $wbState['kwh'] = 0;
                $wbState['daily_last_ts'] = $nowTs;
            } else {
                $dailyDt = $nowTs - (int)($wbState['daily_last_ts'] ?? $wbState['last_ts'] ?? $nowTs);
                if ($dailyDt > 0 && $dailyDt < 3600 && $currentWbPower > 50.0) {
                    $wbState['daily_kwh'] = max(0.0, (float)($wbState['daily_kwh'] ?? 0)) + ($currentWbPower * $dailyDt) / 3600000;
                }
                $wbState['daily_last_ts'] = $nowTs;
                if ($nativeWbSession !== null && (float)$nativeWbSession > 0.001) {
                    $startKwh = $wbState['start_kwh'] ?? 0;
                    if ((float)$nativeWbSession < $startKwh) { $wbState['start_kwh'] = 0; $startKwh = 0; }
                    $wbState['kwh'] = (float)$nativeWbSession - $startKwh;
                } else {
                    $dt = $nowTs - $wbState['last_ts'];
                    if ($dt > 0 && $dt < 3600) { $wbState['kwh'] += ($currentWbPower * $dt) / 3600000; }
                }
                $wbState['last_ts'] = $nowTs;
            }
        } else {
            if ($wbState['is_locked']) {
                // Grace-Period: Session wird erst nach 90s zuverlässig fehlender Verbindung beendet.
                // Kurze RSCP-Aussetzer (Polling-Kollision, ~1-5s) sollen die Session NICHT beenden!
                $WB_GRACE_SECS = 90;
                if (empty($wbState['unlock_ts'])) {
                    $wbState['unlock_ts'] = $nowTs;  // Erster Moment ohne Stecker
                }
                $unlockedFor = $nowTs - $wbState['unlock_ts'];

                if ($unlockedFor >= $WB_GRACE_SECS) {
                    // Auto ist wirklich abgesteckt - Session abschliessen
                    if ($wbState['kwh'] > 0.01 && !empty($wbState['start_ts'])) {
                        if (!file_exists($wbCsvFile)) { @file_put_contents($wbCsvFile, "Timestamp;Start;End;kWh;WB\n"); @chmod($wbCsvFile, 0666); }
                        $endTs = date('Y-m-d H:i:s', $wbState['last_ts']);
                        $logEntry = date('Y-m-d H:i:s', $nowTs) . ";" . $wbState['start_ts'] . ";" . $endTs . ";" . number_format($wbState['kwh'], 2, '.', '') . ";1\n";
                        @file_put_contents($wbCsvFile, $logEntry, FILE_APPEND);
                    }
                    $wbState['is_locked'] = false;
                    $wbState['kwh'] = 0;
                    $wbState['unlock_ts'] = 0;
                }
// Während Grace-Period: Session-kWh unverändert behalten (keine neuen Messungen)
            }
        }
        $wbSessionKwh = $wbState['kwh'];
        @file_put_contents($wbStateFile, json_encode($wbState));
        @chmod($wbStateFile, 0664);
        @flock($wbFp, LOCK_UN); @fclose($wbFp);
    } else {
        if ($wbFp) @fclose($wbFp);
        if (file_exists($wbStateFile)) {
            $parsed = @json_decode(file_get_contents($wbStateFile), true);
            $wbSessionKwh = $parsed['kwh'] ?? 0;
        }
    }
    $data['wb_session_kwh'] = round($wbSessionKwh, 2);
    if ($wbNativeType !== 'openwb' && $wbNativeType !== 'openwb_pro' && $wbNativeEnable) {
        $closedTodayKwh = liveWallboxClosedSessionsTodayKwh($wbCsvFile, 1);
        $activeTodayKwh = isset($wbState) && is_array($wbState)
            ? max(0.0, (float)($wbState['daily_kwh'] ?? $wbSessionKwh))
            : max(0.0, (float)$wbSessionKwh);
        $nativeDailyKwh = round($closedTodayKwh + $activeTodayKwh, 3);
        if ($nativeDailyKwh > 0.001) {
            $data['wb_daily_kwh'] = round($nativeDailyKwh, 2);
            $data['wb_daily_source'] = 'native_session_integrated';
            liveSetExactCounter($data, 'e_wb', $nativeDailyKwh, 'native_session_integrated', 80);
        }
    }
    } else {
        $data['wb_session_kwh'] = 0.0;
    }

    // --- Wallbox 2 Tracking ---
    if ($wb2Configured) {
    $wb2LockFile = '/var/www/html/tmp/wb2_session.lock';
    $wb2Fp = @fopen($wb2LockFile, 'c+');
    if ($wb2Fp && @flock($wb2Fp, LOCK_EX | LOCK_NB)) {
        $wb2State = ['is_locked' => false, 'start_ts' => '', 'last_ts' => $nowTs, 'kwh' => 0];
        if (file_exists($wb2StateFile)) {
            $parsed2 = @json_decode(file_get_contents($wb2StateFile), true);
            if (is_array($parsed2)) $wb2State = array_merge($wb2State, $parsed2);
        }
        if ($isLocked2) {
            if (!$wb2State['is_locked']) {
                $wb2State['is_locked'] = true;
                $wb2State['start_ts'] = date('Y-m-d H:i:s', $nowTs);
                $wb2State['last_ts'] = $nowTs; $wb2State['kwh'] = 0;
            } else {
                $dt = $nowTs - $wb2State['last_ts'];
                if ($dt > 0 && $dt < 3600) { $wb2State['kwh'] += ($currentWb2Power * $dt) / 3600000; }
                $wb2State['last_ts'] = $nowTs;
            }
        } else {
            if ($wb2State['is_locked']) {
                if ($wb2State['kwh'] > 0.01 && !empty($wb2State['start_ts'])) {
                    if (!file_exists($wbCsvFile)) { @file_put_contents($wbCsvFile, "Timestamp;Start;End;kWh;WB\n"); @chmod($wbCsvFile, 0666); }
                    $endTs = date('Y-m-d H:i:s', $wb2State['last_ts']);
                    $logEntry = date('Y-m-d H:i:s', $nowTs) . ";" . $wb2State['start_ts'] . ";" . $endTs . ";" . number_format($wb2State['kwh'], 2, '.', '') . ";2\n";
                    @file_put_contents($wbCsvFile, $logEntry, FILE_APPEND);
                }
                $wb2State['is_locked'] = false; $wb2State['kwh'] = 0;
            }
        }
        $wb2SessionKwh = $wb2State['kwh'];
        @file_put_contents($wb2StateFile, json_encode($wb2State));
        @chmod($wb2StateFile, 0664);
        @flock($wb2Fp, LOCK_UN); @fclose($wb2Fp);
    } else {
        if ($wb2Fp) @fclose($wb2Fp);
        if (file_exists($wb2StateFile)) {
            $parsed2 = @json_decode(file_get_contents($wb2StateFile), true);
            $wb2SessionKwh = $parsed2['kwh'] ?? 0;
        }
    }
    $data['wb2_session_kwh'] = round($wb2SessionKwh, 2);
    } else {
        $data['wb2_session_kwh'] = 0.0;
    }

} elseif ($wbConfigured && file_exists($wbStateFile)) {
    // Fallback falls validData false
    $parsed = @json_decode(file_get_contents($wbStateFile), true);
    $data['wb_session_kwh'] = round($parsed['kwh'] ?? 0, 2);
    if ($wb2Configured && file_exists($wb2StateFile)) {
        $parsed2 = @json_decode(file_get_contents($wb2StateFile), true);
        $data['wb2_session_kwh'] = round($parsed2['kwh'] ?? 0, 2);
    }
} elseif ($wb2Configured && file_exists($wb2StateFile)) {
    $parsed2 = @json_decode(file_get_contents($wb2StateFile), true);
    $data['wb2_session_kwh'] = round($parsed2['kwh'] ?? 0, 2);
}

// --- MQTT/HA Eingangs-Telemetrie: kontrollierte Smart-Home-Bruecke ---
// Der MQTT-Hub akzeptiert nur Allowlist-Werte (z.B. WP-Leistung/Temperaturen)
// und schreibt sie atomar in diese Ramdisk-Datei. Keine MQTT-Befehle greifen
// direkt in RSCP oder systemd ein. Diese Stelle liegt bewusst VOR Live-History,
// damit Frontend, Verbrauchsstatistik und Prognose dieselben Zusatzdaten nutzen.
$mqttHaInboundEnabled = !isset($confData['config']['mqtt_ha_inbound_enable'])
    || in_array(strtolower(trim((string)$confData['config']['mqtt_ha_inbound_enable'])), ['1', 'true', 'yes', 'on'], true);
$mqttHaInboundHistoryEnabled = !isset($confData['config']['mqtt_ha_inbound_history_enable'])
    || in_array(strtolower(trim((string)$confData['config']['mqtt_ha_inbound_history_enable'])), ['1', 'true', 'yes', 'on'], true);
$data['mqtt_ha_inbound_fresh'] = false;
$data['mqtt_ha_inbound_history'] = $mqttHaInboundHistoryEnabled;
$mqttHaAppliedConsumer = false;

$mqttHaInboundFile = '/var/www/html/ramdisk/mqtt_ha_inbound.json';
if ($mqttHaInboundEnabled && file_exists($mqttHaInboundFile) && (time() - filemtime($mqttHaInboundFile) < 180)) {
    $mqttIn = @json_decode(file_get_contents($mqttHaInboundFile), true);
    if ($mqttIn && is_array($mqttIn) && e3dcPayloadContextValid($mqttIn)) {
        $data['mqtt_ha_inbound_fresh'] = true;
        $sources = (isset($mqttIn['sources']) && is_array($mqttIn['sources'])) ? $mqttIn['sources'] : [];
        $sourceFresh = function($source, $key = null) {
            if (!is_array($source)) return false;
            if ($key !== null && isset($source['_updated']) && is_array($source['_updated']) && isset($source['_updated'][$key])) {
                return (time() - (int)$source['_updated'][$key]) < 180;
            }
            if (!isset($source['ts'])) return true;
            return (time() - (int)$source['ts']) < 180;
        };
        $getHaValue = function($source, $key) use ($sourceFresh) {
            if (!is_array($source) || !array_key_exists($key, $source) || !$sourceFresh($source, $key)) return null;
            return $source[$key];
        };
        $haBool = function($value) {
            if (is_bool($value)) return $value;
            return in_array(strtolower(trim((string)$value)), ['1', 'true', 'yes', 'ja', 'on', 'ein', 'active', 'aktiv'], true);
        };

        $haWp = (isset($sources['heatpump']) && is_array($sources['heatpump'])) ? $sources['heatpump'] : [];
        if ($haWp && $sourceFresh($haWp)) {
            $haWpPowerRaw = $getHaValue($haWp, 'power_w');
            if ($haWpPowerRaw === null) $haWpPowerRaw = $getHaValue($haWp, 'electric_w');
            $haWpPower = is_numeric($haWpPowerRaw) ? max(0, (float)$haWpPowerRaw) : null;
            if ($haWpPower !== null) {
                $data['wp'] = (int)round($haWpPower);
                $data['wp_electric_w'] = (float)$haWpPower;
                $data['wp_source'] = 'mqtt_ha';
                $mqttHaAppliedConsumer = true;
            }
            $haVal = $getHaValue($haWp, 'ww_temp'); if ($data['wp_ww_temp'] === null && $haVal !== null) $data['wp_ww_temp'] = (float)$haVal;
            $haVal = $getHaValue($haWp, 'ww_target_temp'); if ($data['wp_ww_soll'] === null && $haVal !== null) $data['wp_ww_soll'] = (float)$haVal;
            $haVal = $getHaValue($haWp, 'flow_temp'); if ($data['wp_vl_temp'] === null && $haVal !== null) $data['wp_vl_temp'] = (float)$haVal;
            $haVal = $getHaValue($haWp, 'return_temp'); if ($data['wp_rl_temp'] === null && $haVal !== null) $data['wp_rl_temp'] = (float)$haVal;
            $haVal = $getHaValue($haWp, 'outside_temp'); if ($data['wp_zuluft_temp'] === null && $haVal !== null) $data['wp_zuluft_temp'] = (float)$haVal;
            $haVal = $getHaValue($haWp, 'heat_kw'); if ($data['wp_heat_kw'] === null && $haVal !== null) $data['wp_heat_kw'] = (float)$haVal;
            $haVal = $getHaValue($haWp, 'mode'); if (!isset($data['wp_mode_text']) && $haVal !== null) $data['wp_mode_text'] = (string)$haVal;
            $haVal = $getHaValue($haWp, 'boost_active'); if (!empty($haVal)) $data['wp_boost_active'] = true;
        }

        $haHeater = (isset($sources['heater']) && is_array($sources['heater'])) ? $sources['heater'] : [];
        if ($haHeater && $sourceFresh($haHeater)) {
            $haHeaterPowerRaw = $getHaValue($haHeater, 'power_w');
            $haHeaterPower = is_numeric($haHeaterPowerRaw) ? max(0, (float)$haHeaterPowerRaw) : null;
            if ($haHeaterPower !== null && ($haHeaterPower > 0 || (int)($data['hs_power'] ?? 0) <= 0)) {
                $data['hs_power'] = (int)round($haHeaterPower);
                $data['Heizstab_Power'] = (int)round($haHeaterPower);
                $data['hs_source'] = 'mqtt_ha';
                $mqttHaAppliedConsumer = true;
            }
            $haVal = $getHaValue($haHeater, 'water_temp');
            if ($haVal !== null && !isset($data['elwa_water_temp_c'])) {
                $data['elwa_water_temp_c'] = (float)$haVal;
            }
            $haVal = $getHaValue($haHeater, 'target_temp');
            if ($haVal !== null && !isset($data['elwa_target_temp_c'])) {
                $data['elwa_target_temp_c'] = (float)$haVal;
            }
        }

        $applyHaWallbox = function($haWb, $wbNo, $configured) use (&$data, &$mqttHaAppliedConsumer, $sourceFresh, $getHaValue, $haBool) {
            if (!$configured || !$haWb || !$sourceFresh($haWb)) return;
            $prefix = ($wbNo === 2) ? 'wb2' : 'wb';
            $haVal = $getHaValue($haWb, 'power_w');
            if ($haVal !== null && is_numeric($haVal)) {
                $power = max(0, (float)$haVal);
                $data[$prefix] = (int)round($power);
                $data['is_external_' . $prefix] = true;
                $data[$prefix . '_source'] = 'mqtt_ha';
                $mqttHaAppliedConsumer = true;
            }
            $haVal = $getHaValue($haWb, 'plugged');
            if ($haVal !== null) {
                $data[$prefix . '_plug'] = $haBool($haVal);
                $data[$prefix . '_locked'] = $data[$prefix . '_plug'];
            }
            $haVal = $getHaValue($haWb, 'charging');
            if ($haVal !== null) {
                $data[$prefix . '_charging'] = $haBool($haVal);
            } elseif (($data[$prefix] ?? 0) > 50) {
                $data[$prefix . '_charging'] = true;
            }
            $haVal = $getHaValue($haWb, 'soc'); if ($haVal !== null) $data[$prefix . '_soc'] = (float)$haVal;
            $haVal = $getHaValue($haWb, 'range_km'); if ($haVal !== null) $data[$prefix . '_range'] = (float)$haVal;
        };

        $haWb1 = [];
        if (isset($sources['wallbox1']) && is_array($sources['wallbox1'])) {
            $haWb1 = $sources['wallbox1'];
        } elseif (isset($sources['wallbox']) && is_array($sources['wallbox'])) {
            $haWb1 = $sources['wallbox'];
        }
        $haWb2 = (isset($sources['wallbox2']) && is_array($sources['wallbox2'])) ? $sources['wallbox2'] : [];
        $applyHaWallbox($haWb1, 1, $wbConfigured);
        $applyHaWallbox($haWb2, 2, $wb2Configured);
    }
}

// MQTT/HA- und Migrationspfade dürfen vor Home-, History- und Peak-Bildung
// ebenfalls keinen ausdrücklich deaktivierten Slot projizieren.
e3dcApplyWallboxPresenceProjection($data, $wbConfigured, $wb2Configured, $wb2ExplicitlyDisabled);

if ($mqttHaAppliedConsumer) {
    $cleanHome = (float)($data['home_raw'] ?? $data['home'] ?? 0);
    $cleanHome -= max(0, (float)($data['wp'] ?? 0));
    $cleanHome -= max(0, (float)($data['hs_power'] ?? 0));
    $cleanHome -= max(0, (float)($data['climate_power_w'] ?? 0));
    if (!empty($data['is_external_wb'])) $cleanHome -= max(0, (float)($data['wb'] ?? 0));
    if (!empty($data['is_external_wb2'])) $cleanHome -= max(0, (float)($data['wb2'] ?? 0));
    $data['home'] = max(0, $cleanHome);
}

liveApplyE3dcMultiHomeRelationFromConfig($data, $confData);
stabilizeCleanHomePower($data);

// Live-History: letzte 48 Stunden in Ramdisk schreiben (ohne price min/max/slots, mit Haus ohne WP)
// PERFORMANCE-FIX: Nur alle 60 Sekunden schreiben, um Pi Zero zu entlasten
$lastWrite = file_exists($liveHistoryFile) ? filemtime($liveHistoryFile) : 0;
// NEU: Nur schreiben, wenn die Zeit abgelaufen ist UND wir gültige Daten gelesen haben ($validData)
$historySampleValid = !isset($data['rscp_sample_valid']) || !empty($data['rscp_sample_valid']);
if ($validData && $historySampleValid && (time() - $lastWrite) >= 60 && !$isStandby) {
    $historyLockFile = '/var/www/html/tmp/history_write.lock';
    $hFp = @fopen($historyLockFile, 'c+');
    if ($hFp && @flock($hFp, LOCK_EX | LOCK_NB)) {
        $lastWriteCheck = file_exists($liveHistoryFile) ? filemtime($liveHistoryFile) : 0;
        if ((time() - $lastWriteCheck) >= 60) {
// Für Statistik/Langzeit gibt es aktuell keinen eigenen Heizstab-Bucket.
        // Deshalb wird Heizstab/myPV-Verbrauch wie WP-Verbrauch behandelt und
        // aus dem reinen Hausverbrauch herausgerechnet.
        $hsPowerForStats = max(0, (float)($data['hs_power'] ?? 0));
        $wpOnlyForStats = max(0, (float)$data['wp']);
        if (empty($data['mqtt_ha_inbound_history'])) {
            if (($data['hs_source'] ?? '') === 'mqtt_ha') $hsPowerForStats = 0;
            if (($data['wp_source'] ?? '') === 'mqtt_ha') $wpOnlyForStats = 0;
        }
        $wpPowerForStats = $wpOnlyForStats + $hsPowerForStats;
        $climatePowerForStats = max(0, (float)($data['climate_power_w'] ?? 0));
        $realHome = isset($data['home']) && is_numeric($data['home'])
            ? (float)$data['home']
            : ((float)$data['home_raw'] - $wpPowerForStats);
        if (!isset($data['home']) || !is_numeric($data['home'])) {
            $realHome -= $climatePowerForStats;
            if (!empty($data['is_external_wb'])) $realHome -= (float)$data['wb'];
            if (!empty($data['is_external_wb2'])) $realHome -= (float)$data['wb2'];
        }

        $historyLine = [
            'ts' => date('c'),
            'pv' => $data['pv'],
            'pv_total_w' => $data['pv_total_w'] ?? $data['pv'],
            'pv_e3dc_w' => $data['pv_e3dc_w'] ?? $data['pv'],
            'pv_external_w' => $data['pv_external_w'] ?? 0,
            'pv_external_source' => $data['pv_external_source'] ?? 'not_reported',
            'pv_external_power_valid' => !empty($data['pv_external_power_valid']),
            'pv_external_topology_present' => !empty($data['pv_external_topology_present']),
            'pv_external_topology_valid' => !empty($data['pv_external_topology_valid']),
            'pv_external_topology_source' => $data['pv_external_topology_source'] ?? 'none',
            'pv_external_topology_evidence_state' => $data['pv_external_topology_evidence_state'] ?? 'unknown',
            'pv_dc_only_configured' => !empty($data['pv_dc_only_configured']),
            'pv_dc_only_active' => !empty($data['pv_dc_only_active']),
            'pv_external_charge_locked' => !empty($data['pv_external_charge_locked']),
            'pv_external_charge_guard_w' => $data['pv_external_charge_guard_w'] ?? 0,
            'bat' => $data['bat'],
            'home_raw' => $data['home_raw'],
            'home_rscp_raw' => $data['home_rscp_raw'] ?? $data['home_raw'],
            'home_balance' => $data['home_balance'] ?? null,
            'home_delta' => $data['home_delta'] ?? null,
            'home' => max(0, $realHome),  // Hausverbrauch ohne WP und ohne ext. WB
            'home_power_source' => $data['home_power_source'] ?? '',
            'home_power_valid' => !empty($data['home_power_valid']),
            'home_power_independent' => !empty($data['home_power_independent']),
            'grid_power_valid' => !empty($data['grid_power_valid']),
            'rscp_sample_valid' => !empty($data['rscp_sample_valid']),
            'rscp_glitch_reasons' => $data['rscp_glitch_reasons'] ?? [],
            'grid' => $data['grid'],
            'soc' => $data['soc'],
            'wb' => $data['wb'],   // Wallbox 1
            'wb2' => $data['wb2'], // Wallbox 2
            'is_external_wb' => !empty($data['is_external_wb']),
            'is_external_wb2' => !empty($data['is_external_wb2']),
            'wb_home_relation' => $data['wb_home_relation'] ?? '',
            'wb2_home_relation' => $data['wb2_home_relation'] ?? '',
            'wb_suppressed_power_w' => $data['wb_suppressed_power_w'] ?? 0,
            'wb2_suppressed_power_w' => $data['wb2_suppressed_power_w'] ?? 0,
            'wb_native_type' => $data['wb_native_type'] ?? '',
            'wb2_native_type' => $data['wb2_native_type'] ?? '',
            'wb_source' => $data['wb_source'] ?? '',
            'wb2_source' => $data['wb2_source'] ?? '',
            'wp_type' => (string)$wpTypeCfg,
            'wp_source' => $data['wp_source'] ?? (($wpTypeCfg === 4) ? 'stiebel_live' : (($wpTypeCfg === 5) ? 'dimplex_live' : '')),
    'wp' => $wpPowerForStats,   // Wärmepumpe inkl. Heizstab/myPV für Statistik
            'hs' => $hsPowerForStats,   // Diagnose: Heizstab/myPV-Anteil separat nachvollziehbar
            'climate' => $climatePowerForStats,
            'climate_active' => !empty($data['climate_active']),
            'climate_source' => $data['climate_source'] ?? '',
            'climate_phase' => $data['climate_phase'] ?? '',
        'price_ct' => $data['price_ct'],
        'optimization_score' => $data['optimization_score'] ?? null,
        'pure_eco_score' => $data['pure_eco_score'] ?? null,
        // Details speichern für Diagramme
        'dc0_w' => $data['dc0_w'],
        'dc0_v' => $data['dc0_v'],
        'dc1_w' => $data['dc1_w'],
        'dc1_v' => $data['dc1_v'],
        'ac0_w' => $data['ac0_w'],
        'ac1_w' => $data['ac1_w'],
        'ac2_w' => $data['ac2_w'],
        'wb_p1' => $data['wb_p1'],
        'wb_p2' => $data['wb_p2'],
        'wb_p3' => $data['wb_p3'],
        'wb2_p1' => $data['wb2_p1'] ?? 0,
        'wb2_p2' => $data['wb2_p2'] ?? 0,
        'wb2_p3' => $data['wb2_p3'] ?? 0,
        'grid_p1' => $data['grid_p1'],
        'grid_p2' => $data['grid_p2'],
        'grid_p3' => $data['grid_p3'],
        'grid_pm_available' => $data['grid_pm_available'] ?? false,
        'grid_pm_index' => $data['grid_pm_index'] ?? null,
        'grid_pm_source' => $data['grid_pm_source'] ?? '',
        'bat_v' => $data['bat_v'],
        'bat_a' => $data['bat_a'],
        'bat1_v' => $data['bat1_v'],
        'bat1_a' => $data['bat1_a'],
        'wb_locked' => $data['wb_locked'], // Status für Session-Berechnung
        'Wärmemenge Gesamt' => $data['Wärmemenge Gesamt'] ?? 0, // Für Tages-Arbeitszahl
        'Heizleistung Ist' => $data['Heizleistung Ist'] ?? 0,
        'Leistungsaufnahme' => $data['Leistungsaufnahme'] ?? 0,
    ];

    foreach ([
        'Aussentemp',
        'Aussentemperatur',
        'Außentemperatur',
        'forecast_temp_c',
        'wp_vl_temp',
        'wp_vl_soll',
        'wp_rl_temp',
        'wp_ww_temp',
        'wp_zuluft_temp',
        'wp_kaelte_temp',
        'wp_kaelte_soll',
    ] as $wpHistoryKey) {
        if (isset($data[$wpHistoryKey]) && is_numeric($data[$wpHistoryKey])) {
            $historyLine[$wpHistoryKey] = round((float)$data[$wpHistoryKey], 2);
        }
    }

    // Reale Tageszähler anhängen
    if (isset($data['e_pv'])) $historyLine['e_pv'] = $data['e_pv'];
    if (isset($data['e_grid_in'])) $historyLine['e_grid_in'] = $data['e_grid_in'];
    if (isset($data['e_grid_out'])) $historyLine['e_grid_out'] = $data['e_grid_out'];
    if (isset($data['e_bat_in'])) $historyLine['e_bat_in'] = $data['e_bat_in'];
    if (isset($data['e_bat_out'])) $historyLine['e_bat_out'] = $data['e_bat_out'];
    if (isset($data['e_home'])) $historyLine['e_home'] = $data['e_home'];
    if (isset($data['e_wb'])) $historyLine['e_wb'] = $data['e_wb'];
    if (isset($data['e_wb2'])) $historyLine['e_wb2'] = $data['e_wb2'];
    if (isset($data['e_wb_source'])) $historyLine['e_wb_source'] = $data['e_wb_source'];
    if (isset($data['e_wb2_source'])) $historyLine['e_wb2_source'] = $data['e_wb2_source'];
    if (isset($data['e_wp'])) $historyLine['e_wp'] = $data['e_wp'];
    if (isset($data['e_wp_source'])) $historyLine['e_wp_source'] = $data['e_wp_source'];
    if (isset($data['climate_daily_kwh']) && is_numeric($data['climate_daily_kwh'])) $historyLine['e_climate'] = $data['climate_daily_kwh'];

    $line = json_encode($historyLine) . "\n";
    // Immer versuchen zu schreiben (is_writable kann auf manchen Servern falsch sein)
    $appendOk = @file_put_contents($liveHistoryFile, $line, LOCK_EX | FILE_APPEND);
    @chmod($liveHistoryFile, 0664);
    if ($appendOk !== false) {
        $cutoff = time() - ($liveHistoryHours * 3600);
        $content = @file_get_contents($liveHistoryFile);
        if ($content !== false && $content !== '') {
            $lines = explode("\n", trim($content));
            $kept = [];
            foreach ($lines as $ln) {
                if (trim($ln) === '') continue;
                $dec = @json_decode($ln, true);

                // Strikte Prüfung: Behalte nur Zeilen, die gültiges JSON sind UND einen Zeitstempel haben.
                if (is_array($dec) && !empty($dec['ts'])) {
                    $t = strtotime($dec['ts']);
                    // Behalte die Zeile, wenn das Datum gültig ist UND nicht zu alt
                    if ($t !== false && $t >= $cutoff) {
                        $kept[] = $ln;
                    }
                }
                // Alle anderen Zeilen (ungültiges JSON, ohne 'ts', zu alt) werden automatisch verworfen.
            }
            // Nur kürzen wenn wir Zeilen entfernen und nicht alles löschen würden
            if (count($kept) < count($lines) && count($kept) > 0) {
                @file_put_contents($liveHistoryFile, implode("\n", $kept) . "\n", LOCK_EX);
            }

            // --- NEU: Tagesstatistiken (Autarkie, Eigenverbrauch, Verteilung) berechnen ---
            $dailyStats = calculateDailyEnergyStats($kept, ['stiebel_wp_counter' => ($wpTypeCfg === 4)]);
            if (isset($data['wb_daily_kwh']) && is_numeric($data['wb_daily_kwh'])) {
                applyExactWallboxDailyCounter($dailyStats, 1, $data['wb_daily_kwh'], $data['e_wb_source'] ?? 'openwb_daily_imported');
            }
            if (isset($data['wb2_daily_kwh']) && is_numeric($data['wb2_daily_kwh'])) {
                applyExactWallboxDailyCounter($dailyStats, 2, $data['wb2_daily_kwh'], $data['e_wb2_source'] ?? 'openwb_daily_imported');
            }
            $savedDeratingToday = (float)($data['saved_derating_today'] ?? 0);
            $savedInverterToday = (float)($data['saved_inverter_today'] ?? 0);
            if (($savedDeratingToday + $savedInverterToday) > 0.0001) {
                $dailyStats['saved'] = [
                    'derating_today_kwh' => round($savedDeratingToday, 3),
                    'inverter_today_kwh' => round($savedInverterToday, 3),
                    'total_today_kwh' => round($savedDeratingToday + $savedInverterToday, 3),
                ];
// Legacy-Felder für Langzeit-Ansicht und SQLite-Archiver.
                $dailyStats['saved_u'] = $dailyStats['saved']['total_today_kwh'];
                $dailyStats['saved_td'] = $dailyStats['saved']['derating_today_kwh'];
                $dailyStats['saved_wb'] = $dailyStats['saved']['inverter_today_kwh'];
            }
            $savedDeratingTotal = (float)($data['alltime_derating'] ?? 0);
            $savedInverterTotal = (float)($data['alltime_inverter'] ?? 0);
            if (($savedDeratingTotal + $savedInverterTotal) > 0.0001) {
                if (!isset($dailyStats['saved']) || !is_array($dailyStats['saved'])) {
                    $dailyStats['saved'] = [];
                }
                $dailyStats['saved']['derating_total_kwh'] = round($savedDeratingTotal, 3);
                $dailyStats['saved']['inverter_total_kwh'] = round($savedInverterTotal, 3);
                $dailyStats['saved']['total_alltime_kwh'] = round($savedDeratingTotal + $savedInverterTotal, 3);
                $dailyStats['alltime_derating'] = $dailyStats['saved']['derating_total_kwh'];
                $dailyStats['alltime_inverter'] = $dailyStats['saved']['inverter_total_kwh'];
                $dailyStats['alltime_total'] = $dailyStats['saved']['total_alltime_kwh'];
                if (isset($data['alltime_start_date'])) {
                    $dailyStats['alltime_start_date'] = $data['alltime_start_date'];
                }
            }
            e3dcApplyEegRevenueToDailyStats($dailyStats, $confData['config'] ?? []);
            file_put_contents('/var/www/html/ramdisk/daily_stats.json', json_encode($dailyStats));
            @chmod('/var/www/html/ramdisk/daily_stats.json', 0664);

        }
            }
        }
        @flock($hFp, LOCK_UN);
        @fclose($hFp);
    } elseif ($hFp) {
        @fclose($hFp);
    }
}

// Statistiken laden (wird oben alle 60s berechnet)
$statsFile = '/var/www/html/ramdisk/daily_stats.json';
if (file_exists($statsFile)) {
    $statsData = json_decode(file_get_contents($statsFile), true);
    if ($statsData) {
        e3dcApplyEegRevenueToDailyStats($statsData, $confData['config'] ?? []);
        if ($wpTypeCfg === 4 && isset($statsData['stats']) && is_array($statsData['stats'])) {
            $historyForDisplay = file_exists($liveHistoryFile)
                ? (@file($liveHistoryFile, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) ?: [])
                : [];
            $displayWpKwh = liveStiebelInterpolatedWpDayKwh($historyForDisplay, $data);
            if ($displayWpKwh !== null) {
                $data['wp_daily_display_kwh'] = $displayWpKwh;
                $rawWpCounter = (float)($data['e_wp'] ?? 0);
                if ($displayWpKwh > $rawWpCounter + 0.05) {
                    $data['e_wp'] = round($displayWpKwh, 3);
                    $data['e_wp_source'] = 'stiebel_external_power_integrated';
                    $statsData['stats']['total_wp_counter_kwh'] = round($displayWpKwh, 3);
                } else {
                    $statsData['stats']['total_wp_counter_kwh'] = round((float)($data['e_wp'] ?? $statsData['stats']['total_wp_kwh'] ?? 0), 3);
                }
                $statsData['stats']['total_wp_display_kwh'] = round($displayWpKwh, 2);
            }
        }
        $data['pv_today_kwh'] = $statsData['pv_today_kwh'] ?? null;
        $data['autarky_day'] = $statsData['autarky_day'] ?? null;
        $data['selfcon_day'] = $statsData['selfcon_day'] ?? null;
        $data['stats'] = $statsData['stats'] ?? null;
        $data['costs'] = $statsData['costs'] ?? null;
    }
}

// CPU Load (1 min average)
$load = function_exists('sys_getloadavg') ? @sys_getloadavg() : false;
if (is_array($load) && isset($load[0]) && is_numeric($load[0])) {
    $data['cpu_load'] = (float)$load[0];
}

// CPU Temp
$temp_file = '/sys/class/thermal/thermal_zone0/temp';
if (is_readable($temp_file)) {
    $temp = (int)file_get_contents($temp_file);
    $data['cpu_temp'] = $temp / 1000.0;
}

// --- NEU: Auto-SoC in den Live-Stream einmischen ---
$vehiclesFile = '/var/www/html/ramdisk/vehicles.json';
if (file_exists($vehiclesFile)) {
    $vData = @json_decode(file_get_contents($vehiclesFile), true);
    $bluelinkRefresh = vehicleBluelinkRefreshProjection($vData);
    if ($bluelinkRefresh !== null) {
        $data['bluelink_refresh'] = $bluelinkRefresh;
        $data['bluelink_source_ts'] = $bluelinkRefresh['source_ts'];
        $data['bluelink_source_age_s'] = $bluelinkRefresh['source_age_s'];
    }
    if ($vData && isset($vData['vehicles'])) {
        $data['vehicles'] = $vData['vehicles'];
        foreach ($data['vehicles'] as &$cloudVeh) {
            if (is_array($cloudVeh)) {
                if (empty($cloudVeh['soc_source'])) {
                    $cloudVeh['soc_source'] = 'legacy_vehicle_unknown';
                    if (!array_key_exists('soc_source_ts', $cloudVeh)) {
                        $cloudVeh['soc_source_ts'] = null;
                        $cloudVeh['last_updated_at'] = null;
                    }
                    $cloudVeh['soc_rule_confirmed'] = false;
                    $cloudVeh['soc_stale'] = true;
                }
                $vehicleSource = strtolower(trim((string)$cloudVeh['soc_source']));
                if (in_array($vehicleSource, ['bluelink', 'hyundai', 'kia', 'cloud', 'vehicle_cloud'], true)
                    && empty($cloudVeh['cloud_vehicle_id']) && !empty($cloudVeh['id'])) {
                    $cloudVeh['cloud_vehicle_id'] = $cloudVeh['id'];
                }
            }
        }
        unset($cloudVeh);
    }
    if ($vData && !empty($vData['error'])) {
        $data['car_error'] = $vData['error'];
    } elseif ($bluelinkRefresh !== null
        && !empty($bluelinkRefresh['last_error_active'])
        && !empty($bluelinkRefresh['last_error']['message'])) {
        // Ein späterer Cached-Abruf kann technisch erfolgreich sein und
        // trotzdem denselben alten Fahrzeugstand liefern. In diesem Fall
        // bleibt der erfolglose Wakeup sichtbar, bis der Quellanker fortschreitet.
        $data['car_error'] = $bluelinkRefresh['last_error']['message'];
    }
}

// --- openWB: Fahrzeug aus openwb_data.json einmischen (SoC via CCS/MQTT) ---
// Das openWB-Fahrzeug wird IMMER eingemischt wenn wb_soc > 0 (unabhaengig von vehicles[]).
// Fehlende Kapazitaet (car_capacity_kwh=0) wird per Namens-Match aus saved_cars ergaenzt.
$owbCarName = '';
$owbCarId   = null;
$owbVehicleId = null;
$owbCapKwh  = 0.0;
$owbConsumptionKwh100 = 0.0;
$owbWbSoc   = vehicleSocPercentValue($data['wb_soc'] ?? null);
$owbWbSocConfirmed = $owbWbSoc !== null
    && ($data['wb_soc_rule_confirmed'] ?? null) === true;
$owbWbRange = isset($data['wb_range']) ? (float)$data['wb_range'] : 0.0;
$owbWbPlug  = isset($data['wb_plug']) ? (bool)$data['wb_plug'] : false;
$owbStableIdentityCurrent = !empty($data['wb_stable_vehicle_identity_current']);

$openwbDataFile2 = '/var/www/html/ramdisk/openwb_data.json';
if (file_exists($openwbDataFile2)) {
    $owbD = @json_decode(file_get_contents($openwbDataFile2), true);
    if (is_array($owbD)) {
        $owbCarName = trim($owbD['car_name'] ?? '');
        $owbCarId   = $owbD['car_id'] ?? null;
        $owbVehicleId = $owbD['vehicle_id'] ?? null;
        $owbCapKwh  = (float)($owbD['car_capacity_kwh'] ?? 0);
    }
}

// --- Custom Saved Cars laden (für Namens-Match & Kapazität) ---
$savedCarsFile = '/var/www/html/data/saved_cars.json';
$savedCars = [];
if (file_exists($savedCarsFile)) {
    $sc_raw = @json_decode(file_get_contents($savedCarsFile), true);
    if (is_array($sc_raw)) $savedCars = $sc_raw;
}

// Eindeutige Konfigurations-Zuordnung für die Wallbox-Seite. Die Live-Felder
// wb*_car_id koennen von openWB/openWB Pro belegt oder leer sein.
foreach ([1, 2] as $assignSlot) {
    $assignKey = "wb{$assignSlot}_car_id";
    $selection = trim((string)($confData['config'][$assignKey] ?? ''));
    $car = savedCarForWallboxSelection($savedCars, $selection);
    $assignedId = $car['id'] ?? $selection;
    if ($assignedId === '') $assignedId = '__none';
    $data["wb{$assignSlot}_assigned_car_id"] = $assignedId;
    $data["wb{$assignSlot}_assigned_car_name"] = $car['name'] ?? '';
    $data["wb{$assignSlot}_assigned_vehicle_id"] = $car['vehicle_id'] ?? null;
    $data["wb{$assignSlot}_assigned_cloud_vehicle_id"] = $car['cloud_vehicle_id'] ?? null;

    $runtimePrefix = $assignSlot === 1 ? 'wb' : 'wb2';
    $liveStableIdentifiers = array_values(array_filter([
        $data["{$runtimePrefix}_live_vehicle_id"] ?? null,
        $data["{$runtimePrefix}_live_rfid_tag"] ?? null,
        $data["{$runtimePrefix}_live_car_id"] ?? null,
    ], function($value) { return trim((string)$value) !== ''; }));
    $stableIdentityCurrent = !empty($data["{$runtimePrefix}_stable_vehicle_identity_current"]);
    if (wallboxSlotLooksConnected($data, $assignSlot)
        && trim((string)($data["{$runtimePrefix}_display_car_name"] ?? '')) === ''
        && trim((string)($car['name'] ?? '')) !== '') {
        $data["{$runtimePrefix}_display_car_name"] = trim((string)$car['name']);
        $data["{$runtimePrefix}_display_car_source"] = 'e3dc_config_fallback';
    }
    $data["wb{$assignSlot}_vehicle_identity"] = [
        'assigned' => [
            'profile_id' => $assignedId,
            'name' => $car['name'] ?? '',
            'vehicle_id' => $car['vehicle_id'] ?? null,
            'source' => 'e3dc_config',
        ],
        'live_vehicle' => [
            'name' => $data["{$runtimePrefix}_live_car_name"] ?? '',
            'car_id' => $data["{$runtimePrefix}_live_car_id"] ?? null,
            'vehicle_id' => $data["{$runtimePrefix}_live_vehicle_id"] ?? null,
            'rfid_tag' => $data["{$runtimePrefix}_live_rfid_tag"] ?? null,
            'stable_identity_present' => $stableIdentityCurrent,
            'retained_identity_present' => !empty($liveStableIdentifiers),
            'identity_current' => $stableIdentityCurrent,
            'source' => 'openwb_runtime',
        ],
        'charge_profile' => [
            'name' => $data["{$runtimePrefix}_charge_profile_name"] ?? '',
            'source' => $data["{$runtimePrefix}_charge_profile_source"] ?? '',
            'vehicle_identity' => false,
        ],
        'observe_only' => true,
        'config_changed_from_live' => false,
    ];
}

// Kapazität aus saved_cars nur bei exakter stabiler Kennung oder exakt normalisiertem Namen ergänzen.
$owbMatchedSavedId = null;
$owbVehicleInjected = false;
$owbMatchedCar = savedCarForVehicleIdentifiers(
    $savedCars,
    [
        'vehicle_id' => $owbStableIdentityCurrent ? ($owbVehicleId ?? '') : '',
        'car_id' => $owbStableIdentityCurrent ? ($owbCarId ?? '') : '',
        'rfid_tag' => $owbStableIdentityCurrent ? ($data['wb_rfid_tag'] ?? '') : '',
    ],
    $owbStableIdentityCurrent ? $owbCarName : ''
);
if ($owbMatchedCar) {
    if ($owbCapKwh <= 0) $owbCapKwh = (float)($owbMatchedCar['capacity'] ?? 0);
    $owbConsumptionKwh100 = (float)($owbMatchedCar['consumption'] ?? $owbMatchedCar['consumption_kwh_100km'] ?? $owbMatchedCar['avg_consumption'] ?? 0);
    $owbMatchedSavedId = $owbMatchedCar['id'] ?? null;
}

if (!isset($data['vehicles'])) $data['vehicles'] = [];
injectConfiguredWallboxVehicle($data, $savedCars, $confData['config'] ?? [], 1);
injectConfiguredWallboxVehicle($data, $savedCars, $confData['config'] ?? [], 2);

// Aktuelle openWB-Anzeige als erstes Fahrzeug eintragen. Ohne aktuell stabile
// ID darf ein erhaltener Kia-/Honda-SoC nicht dem beobachteten Gastprofil
// zugeschrieben werden.
$owbDisplayName = trim((string)($data['wb_display_car_name'] ?? ''));
$owbActiveVehicle = wallboxSlotLooksConnected($data, 1);
if ($owbActiveVehicle && ($owbWbSocConfirmed || $owbDisplayName !== '')) {
    $displayName = $owbDisplayName !== '' ? $owbDisplayName : ($owbCarName ?: 'openWB');
    $vehicleId   = $owbMatchedSavedId ?: ($owbStableIdentityCurrent && $owbCarId ? $owbCarId : 'openwb_observed_wb1');
    $displaySoc = $owbStableIdentityCurrent && $owbWbSocConfirmed ? $owbWbSoc : null;
    array_unshift($data['vehicles'], [
        'id'              => $vehicleId,
        'profile_id'      => $owbMatchedSavedId,
        'name'            => $displayName,
        'soc'             => $displaySoc,
        'range_km'        => $owbWbRange > 0 ? $owbWbRange : null,
        'car_range_source' => $data['wb_range_source'] ?? '',
        'car_range_valid' => ($data['wb_range_valid'] ?? false) === true,
        'car_range_observed_ts' => $data['wb_range_observed_ts'] ?? null,
        'car_range_source_ts' => $data['wb_range_source_ts'] ?? null,
        'car_range_source_ts_explicit' => ($data['wb_range_source_ts_explicit'] ?? false) === true,
        'car_range_vehicle_key' => $data['wb_range_vehicle_key'] ?? '',
        'capacity_kwh'    => $owbCapKwh > 0 ? $owbCapKwh : null,
        'consumption_kwh_100km' => $owbConsumptionKwh100 > 0 ? $owbConsumptionKwh100 : null,
        'is_plugged_in'   => $owbWbPlug,
        'is_charging'     => $owbWbPlug && (($data['wb'] ?? 0) > 50),
        'soc_source'      => !empty($data['wb_soc_source']) ? $data['wb_soc_source'] : 'ccs_wallbox',
        'soc_rule_confirmed' => $displaySoc !== null && ($data['wb_soc_rule_confirmed'] ?? null) === true,
        'soc_age_contract' => $data['wb_soc_age_contract'] ?? null,
        'soc_age_contract_source' => $data['wb_soc_age_contract_source'] ?? null,
        'soc_max_age_s' => $data['wb_soc_max_age_s'] ?? null,
        'wb_slot'         => 1,
        'car_id'          => $owbStableIdentityCurrent ? ($data['wb_car_id'] ?? ($owbCarId ?? null)) : null,
        'rfid_tag'        => $owbStableIdentityCurrent ? ($data['wb_rfid_tag'] ?? null) : null,
        'vehicle_id'      => $owbStableIdentityCurrent ? ($data['wb_vehicle_id'] ?? ($owbVehicleId ?? null)) : null,
        'stable_vehicle_identity_current' => $owbStableIdentityCurrent,
        'is_manual'       => false,
        'is_interpolated' => strpos((string)($data['wb_soc_source'] ?? ''), 'estimated') !== false,
        'soc_source_ts'   => vehicleSocTimestamp($data['wb_soc_source_ts'] ?? null),
        'last_updated_at' => vehicleSocTimestamp($data['wb_soc_source_ts'] ?? null)
    ]);
    $owbVehicleInjected = true;
}

// openWB Pro / zweite openWB: Fahrzeugkennung, SoC und RFID für WB2 einmischen.
$owb2WbSoc = vehicleSocPercentValue($data['wb2_soc'] ?? null);
$owb2WbSocConfirmed = $owb2WbSoc !== null
    && ($data['wb2_soc_rule_confirmed'] ?? null) === true;
$owb2MatchedSavedId = null;
$owb2VehicleInjected = false;
$owb2ActiveVehicle = !empty($data['wb2_plug']) || !empty($data['wb2_charging']) || abs((float)($data['wb2'] ?? 0)) > 50;
$owb2StableIdentityCurrent = !empty($data['wb2_stable_vehicle_identity_current']);
$owb2DisplayName = trim((string)($data['wb2_display_car_name'] ?? ''));
if ($owb2ActiveVehicle && ($owb2WbSocConfirmed || $owb2DisplayName !== '' || !empty($data['wb2_car_name']))) {
    $owb2CarName = trim((string)($data['wb2_car_name'] ?? ''));
    $owb2CarId = $data['wb2_car_id'] ?? ($data['wb2_vehicle_id'] ?? ($data['wb2_rfid_tag'] ?? 'openwb_pro_wb2'));
    $owb2CapKwh = 0.0;
    $owb2ConsumptionKwh100 = 0.0;
    $owb2MatchedCar = savedCarForVehicleIdentifiers(
        $savedCars,
        [
            'vehicle_id' => $owb2StableIdentityCurrent ? ($data['wb2_vehicle_id'] ?? '') : '',
            'rfid_tag' => $owb2StableIdentityCurrent ? ($data['wb2_rfid_tag'] ?? '') : '',
            'car_id' => $owb2StableIdentityCurrent ? ($owb2CarId ?? '') : '',
        ],
        $owb2StableIdentityCurrent ? $owb2CarName : ''
    );
    if ($owb2MatchedCar) {
        $owb2CapKwh = (float)($owb2MatchedCar['capacity'] ?? 0);
        $owb2ConsumptionKwh100 = (float)($owb2MatchedCar['consumption'] ?? $owb2MatchedCar['consumption_kwh_100km'] ?? $owb2MatchedCar['avg_consumption'] ?? 0);
        $owb2MatchedSavedId = $owb2MatchedCar['id'] ?? null;
    }
    $wb2DisplaySoc = $owb2StableIdentityCurrent && $owb2WbSocConfirmed ? $owb2WbSoc : null;
    $data['vehicles'][] = [
        'id'              => $owb2MatchedSavedId ?: ($owb2StableIdentityCurrent ? $owb2CarId : 'openwb_observed_wb2'),
        'profile_id'      => $owb2MatchedSavedId,
        'name'            => $owb2DisplayName !== '' ? $owb2DisplayName : ($owb2CarName ?: 'openWB Pro'),
        'soc'             => $wb2DisplaySoc,
        'range_km'        => !empty($data['wb2_range']) ? (float)$data['wb2_range'] : null,
        'car_range_source' => $data['wb2_range_source'] ?? '',
        'car_range_valid' => ($data['wb2_range_valid'] ?? false) === true,
        'car_range_observed_ts' => $data['wb2_range_observed_ts'] ?? null,
        'car_range_source_ts' => $data['wb2_range_source_ts'] ?? null,
        'car_range_source_ts_explicit' => ($data['wb2_range_source_ts_explicit'] ?? false) === true,
        'car_range_vehicle_key' => $data['wb2_range_vehicle_key'] ?? '',
        'capacity_kwh'    => $owb2CapKwh > 0 ? $owb2CapKwh : null,
        'consumption_kwh_100km' => $owb2ConsumptionKwh100 > 0 ? $owb2ConsumptionKwh100 : null,
        'is_plugged_in'   => !empty($data['wb2_plug']),
        'is_charging'     => !empty($data['wb2_charging']) || (($data['wb2'] ?? 0) > 50),
        'soc_source'      => !empty($data['wb2_soc_source']) ? $data['wb2_soc_source'] : 'ccs_wallbox_wb2',
        'soc_rule_confirmed' => $wb2DisplaySoc !== null && ($data['wb2_soc_rule_confirmed'] ?? null) === true,
        'soc_age_contract' => $data['wb2_soc_age_contract'] ?? null,
        'soc_age_contract_source' => $data['wb2_soc_age_contract_source'] ?? null,
        'soc_max_age_s' => $data['wb2_soc_max_age_s'] ?? null,
        'wb_slot'         => 2,
        'car_id'          => $owb2StableIdentityCurrent ? ($data['wb2_car_id'] ?? ($owb2CarId ?? null)) : null,
        'rfid_tag'        => $owb2StableIdentityCurrent ? ($data['wb2_rfid_tag'] ?? null) : null,
        'vehicle_id'      => $owb2StableIdentityCurrent ? ($data['wb2_vehicle_id'] ?? null) : null,
        'stable_vehicle_identity_current' => $owb2StableIdentityCurrent,
        'is_manual'       => false,
        'is_interpolated' => strpos((string)($data['wb2_soc_source'] ?? ''), 'estimated') !== false,
        'soc_source_ts'   => vehicleSocTimestamp($data['wb2_soc_source_ts'] ?? null),
        'last_updated_at' => vehicleSocTimestamp($data['wb2_soc_source_ts'] ?? null)
    ];
    $owb2VehicleInjected = true;
}

// saved_cars einmischen — aber NICHT doppelt wenn bereits via openWB eingemischt (Namens-Match)
if (!empty($savedCars)) {
    foreach ($savedCars as &$sc) {
        // Bereits als openWB-Fahrzeug vorhanden? Ueberspringen (kein Duplikat)
        if ($owbVehicleInjected && $owbMatchedSavedId !== null && ($sc['id'] ?? '') === $owbMatchedSavedId) {
            continue;
        }
        if ($owb2VehicleInjected && $owb2MatchedSavedId !== null && ($sc['id'] ?? '') === $owb2MatchedSavedId) {
            continue;
        }
        $sc['soc'] = 0; // Default
        foreach([1, 2] as $idx) {
            $manSoC = "/var/www/html/ramdisk/manual_soc_wb{$idx}.json";
            if (file_exists($manSoC)) {
                $mD = @json_decode(file_get_contents($manSoC), true);
                if (!is_array($mD)) $mD = null;
                if ($mD && !wallboxPayloadSlotMatches($mD, $idx, true)) continue;
                $manualCarId = $mD['car_id'] ?? '';
                $manualProfileId = savedCarIdForVehicleIdentifiers($savedCars, [
                    'profile_id' => $mD['profile_id'] ?? $manualCarId,
                    'car_id' => $manualCarId,
                    'vehicle_id' => $mD['vehicle_id'] ?? '',
                ], $mD['name'] ?? '');
                $manualSlot = $idx;
                $manualSource = (string)($mD['source'] ?? 'manual_soc');
                $manualSourceLower = strtolower(trim($manualSource));
                $manualSourceTs = vehicleSocTimestamp(
                    $mD['soc_source_ts'] ?? ($mD['raw_soc_ts'] ?? null)
                );
                $manualNeedsExplicitAnchor = vehicleSocUsesOpenwbProAnchor($manualSourceLower)
                    || vehicleSocUsesMqttAnchor($manualSourceLower)
                    || vehicleSocSourceClass($manualSourceLower) === 'cloud'
                    || strpos($manualSourceLower, 'wallbox_estimated') === 0;
                if ($manualSourceTs === null && !$manualNeedsExplicitAnchor) {
                    $manualSourceTs = vehicleSocTimestamp($mD['ts'] ?? null);
                }
                $manualProbe = is_array($mD) ? $mD : [];
                $manualProbe['soc_source'] = $manualSource;
                $manualProbe['soc_source_ts'] = $manualSourceTs;
                $manualProbe['profile_id'] = $manualProfileId;
                $manualMeta = vehicleSocTruthMeta(
                    $manualProbe,
                    null,
                    vehicleSocCloudFreshnessSeconds($confData['config'] ?? [])
                );
                $manualSocConfirmed = !vehicleSocExplicitVetoed($mD, true)
                    && !empty($manualMeta['rule_usable']);
                if ($mD
                    && vehicleSocContractFlagActive($mD['soc_profile_binding_invalid'] ?? null)
                    && $manualProfileId !== null
                    && $manualProfileId === ($sc['id'] ?? '')
                    && wallboxSlotLooksConnected($data, $manualSlot)) {
                    $sc['soc_profile_binding_invalid'] = true;
                    $sc['soc_source'] = 'wallbox_estimated_profile_binding_invalid';
                    $sc['soc_rule_confirmed'] = false;
                    $sc['wb_slot'] = $manualSlot;
                    $sc['is_plugged_in'] = true;
                    $sc['is_charging'] = false;
                }
                if ($mD && $manualSocConfirmed && $manualProfileId !== null && $manualProfileId === ($sc['id'] ?? '') && wallboxSlotLooksConnected($data, $manualSlot)) {
                    $sc['soc'] = $mD['soc'];
                    $sc['soc_source'] = $manualSource;
                    $sc['soc_rule_confirmed'] = true;
                    $sc['soc_age_contract'] = $mD['soc_age_contract'] ?? null;
                    $sc['soc_age_contract_source'] = $mD['soc_age_contract_source'] ?? null;
                    $sc['soc_max_age_s'] = $mD['soc_max_age_s'] ?? null;
                    $sc['soc_profile_bound'] = !empty($mD['soc_profile_bound']);
                    $sc['is_interpolated'] = !empty($mD['is_interpolated']) || strpos((string)($mD['source'] ?? ''), 'estimated') !== false;
                    if (!empty($mD['range_km'])) $sc['range_km'] = (float)$mD['range_km'];
                    if (!empty($mD['consumption_kwh_100km'])) $sc['consumption_kwh_100km'] = (float)$mD['consumption_kwh_100km'];
                    $sc['last_updated_at'] = (int)($mD['ts'] ?? time());
                    $sc['soc_source_ts'] = $manualSourceTs;
                    $sc['wb_slot'] = $manualSlot;
                    $sc['is_plugged_in'] = true;
                    $sc['is_charging'] = !empty($mD['charging']);
                }
            }
        }
        $data['vehicles'][] = $sc;
    }
    unset($sc);
}


// Flag-Timeout: falls bluelink_client nicht läuft, Flag nach 5 Min auto-löschen
$_blFlag = '/var/www/html/ramdisk/force_bluelink.flag';
if (file_exists($_blFlag)) {
    if (time() - filemtime($_blFlag) > 300) { // 5 Minuten
        @unlink($_blFlag);
        $data['car_force_running'] = false;
    } else {
        $data['car_force_running'] = true;
    }
} else {
    $data['car_force_running'] = false;
}
$data['has_bluelink'] = e3dcBluelinkRefreshTokenConfigured();


// --- NEU: Virtuelle Lade-Sessions (Interpolation & Restzeit) einmischen für Dual-WB ---
$carChargeSessions = [
    1 => liveCarChargeSessionSnapshot(1),
    2 => liveCarChargeSessionSnapshot(2),
];
$sessions = [
    ['data' => $carChargeSessions[1]['data'], 'wb' => 'wb'],
    ['data' => $carChargeSessions[2]['data'], 'wb' => 'wb2'],
];

foreach ($sessions as $sIndex => $sConf) {
    $sessData = is_array($sConf['data'] ?? null) ? $sConf['data'] : [];
    if ($sessData && !wallboxPayloadSlotMatches($sessData, $sIndex + 1, true)) {
        continue;
    }
    if ($sessData) {
            $vSoc = $sessData['current_virtual_soc'] ?? null;
            $tTar = $sessData['time_to_target_mins'] ?? null;
            $carId = $sessData['car_id'] ?? null;
            $isManual = $sessData['is_manual'] ?? false;
            $sessionSocSource = (string)($sessData['soc_source'] ?? '');
            $sessionTs = (float)($sessData['ts'] ?? 0);
            $sessionSourceTs = vehicleSocTimestamp($sessData['soc_source_ts'] ?? null);
            if ($sessionSourceTs === null) {
                $sessionSourceTs = vehicleSocTimestamp(
                    !empty($sessData['is_manual'])
                        ? ($sessData['last_manual_ts'] ?? null)
                        : ($sessData['last_car_ts'] ?? null)
                );
            }
            $vSoc = vehicleSocPercentValue($vSoc);
            $sessionSocConfirmed = $vSoc !== null
                && !vehicleSocExplicitVetoed($sessData, true)
                && wallboxSocTruthConfirmed(
                    $sessionSocSource,
                    $sessData['soc_rule_confirmed'] ?? null,
                    $sessionSourceTs,
                    null,
                    array_key_exists('soc_rule_confirmed', $sessData),
                    $sessData,
                    vehicleSocCloudFreshnessSeconds($confData['config'] ?? [])
                );
            $customName = !empty($sessData['car_name']) ? $sessData['car_name'] : (($sIndex == 0) ? 'Gast (WB1)' : 'Gast (WB2)');
            $wbPrefix = ($sIndex == 0) ? 'wb' : 'wb2';
            $sessionWbConnected = !empty($data[$wbPrefix . '_plug'])
                || !empty($data[$wbPrefix . '_locked'])
                || !empty($data[$wbPrefix . '_charging'])
                || abs((float)($data[$sConf['wb']] ?? 0)) > 50;
            if (!$sessionWbConnected) {
                continue;
            }
            $sessionProfileId = savedCarIdForVehicleIdentifiers(
                $savedCars ?? [],
                [
                    'profile_id' => $carId,
                    'vehicle_id' => $sessData['vehicle_id'] ?? '',
                    'rfid_tag' => $sessData['rfid_tag'] ?? '',
                ],
                $customName
            );

            // 1. Passendes Fahrzeug in $data['vehicles'] suchen
            $vIdx = -1;
            if (isset($data['vehicles']) && is_array($data['vehicles'])) {
                foreach ($data['vehicles'] as $i => $v) {
                    if ($sessionProfileId && (($v['id'] ?? null) == $sessionProfileId || ($v['profile_id'] ?? null) == $sessionProfileId)) { $vIdx = $i; break; }
                    if ($carId && ($v['id'] ?? null) == $carId) { $vIdx = $i; break; }
                }
                // Alter WB1-Sessionname car1/leer besitzt keine Identität. Er
                // darf nur bei genau einer logischen Fahrzeugidentität binden.
                if ($vIdx == -1 && $sIndex == 0 && (empty($carId) || $carId === 'car1')) {
                    $vIdx = legacySessionSingleVehicleIndex($data['vehicles']);
                }
            }

// CCS-Fahrzeug für diesen WB-Slot vorhanden? Dann kein Proxy-Duplikat erstellen
// und interpolierten SoC NICHT über echten CCS-Wert schreiben.
            $wbSlotHasCcs = false;
            if (isset($data['vehicles'])) {
                foreach ($data['vehicles'] as $chkV) {
                    if ((int)($chkV['wb_slot'] ?? 0) === ($sIndex + 1)
                        && in_array(($chkV['soc_source'] ?? ''), ['ccs_wallbox', 'ccs_wallbox_wb2', 'openwb_pro_raw', 'openwb_pro_estimated', 'openwb_mqtt'], true)) {
                        $wbSlotHasCcs = true;
                        break;
                    }
                }
            }

            if ($vIdx !== -1) {
                $veh = &$data['vehicles'][$vIdx];
                if ($sessionProfileId && empty($veh['profile_id'])) $veh['profile_id'] = $sessionProfileId;
                // Der zyklische Session-Zeitpunkt belegt nur eine frische
                // Projektion. Ob ihr Ist-SoC einen vorhandenen Cloud-/CCS-Wert
                // ersetzen darf, entscheidet ausschließlich der Rohanker.
                $sessionFreshForVehicle = vehicleSocSourceNotOlderThan(
                    $veh,
                    $sessionSourceTs,
                    120
                );
                // Interpolierten SoC NUR uebernehmen, wenn er nicht aelter als echte Cloud-/CCS-Daten ist.
                if ($sessionFreshForVehicle && !$wbSlotHasCcs && $vSoc !== null) {
                    if ($sessionSocConfirmed) {
                        $veh['soc'] = $vSoc;
                        $veh['is_interpolated'] = true;
                        if ($sessionSocSource !== '') {
                            $veh['soc_source'] = $sessionSocSource;
                            $veh['soc_rule_confirmed'] = true;
                        }
                        $veh['soc_source_ts'] = $sessionSourceTs;
                        $veh['soc_age_contract'] = $sessData['soc_age_contract'] ?? null;
                        $veh['soc_age_contract_source'] = $sessData['soc_age_contract_source'] ?? null;
                        $veh['soc_max_age_s'] = $sessData['soc_max_age_s'] ?? null;
                        if ($sessionTs > 0) $veh['last_updated_at'] = (int)$sessionTs;
                    }
                }
                if ($sessionFreshForVehicle && $tTar !== null) $veh['time_to_target_mins'] = $tTar;
                $vehIsCcs = (($veh['soc_source'] ?? '') === 'ccs_wallbox');
                if ($sessionFreshForVehicle && (!$vehIsCcs || $owbWbPlug)) $veh['is_plugged_in'] = true;
                $customNameClean = trim((string)$customName);
                $customNameLooksLikeId =
                    $customNameClean === (string)$carId ||
                    preg_match('/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i', $customNameClean) ||
                    preg_match('/^custom_\d+$/', $customNameClean);
                if ($sessionFreshForVehicle && $isManual && !$sessionProfileId && !empty($customNameClean) && !$wbSlotHasCcs && !$customNameLooksLikeId) {
                    $veh['name'] = $customNameClean;
                }
            } else {
                // Proxy-Fahrzeug (Gast WBx) NUR erstellen wenn kein echtes CCS-Fahrzeug vorhanden
                if (!$wbSlotHasCcs) {
                    if (!isset($data['vehicles'])) $data['vehicles'] = [];
                    $data['vehicles'][] = [
                        'id'                  => $sessionProfileId ?: ($carId ?: ('manual_wb' . ($sIndex + 1))),
                        'name'                => $customName,
                        'soc'                 => $sessionSocConfirmed ? $vSoc : null,
                        'is_plugged_in'       => true,
                        'is_charging'         => ($data[$sConf['wb']] ?? 0) > 50,
                        'time_to_target_mins' => $tTar,
                        'wb_slot'             => $sIndex + 1,
                        'is_interpolated'     => true,
                        'is_manual'           => true,
                        'soc_source'          => $sessionSocSource,
                        'soc_rule_confirmed'  => $sessionSocConfirmed,
                        'soc_source_ts'       => $sessionSourceTs,
                        'soc_age_contract'    => $sessData['soc_age_contract'] ?? null,
                        'soc_age_contract_source' => $sessData['soc_age_contract_source'] ?? null,
                        'soc_max_age_s'       => $sessData['soc_max_age_s'] ?? null,
                        'last_updated_at'     => $sessionTs > 0 ? (int)$sessionTs : null
                    ];
                }
            }
    }
}

// --- Reichweite (km) berechnen, falls nicht von API geliefert ---
if (isset($data['vehicles']) && is_array($data['vehicles'])) {
    $vehicleSocDisplayCacheFile = '/var/www/html/ramdisk/vehicle_soc_display_cache.json';
    $vehicleSocDisplayCache = loadVehicleSocDisplayCache($vehicleSocDisplayCacheFile);
    foreach ($data['vehicles'] as &$veh) {
        applyVehicleSocDisplayCache($veh, $vehicleSocDisplayCache);
    }
    unset($veh);

    $sessData = is_array($carChargeSessions[1]['data'] ?? null)
        ? $carChargeSessions[1]['data']
        : [];
    foreach ($data['vehicles'] as &$veh) {
        if (isset($veh['soc']) && empty($veh['range_km'])) {
            // Prioritaet: 1. capacity_kwh aus Fahrzeug (openWB connected_vehicle/info)
            //             2. Aktive Ladesession  3. Config-Wert  4. Fallback 72 kWh
            if (!empty($veh['capacity_kwh']) && (float)$veh['capacity_kwh'] > 0) {
                $carCapacity = (float)$veh['capacity_kwh'];
            } elseif (!empty($sessData['car_capacity'])) {
                $carCapacity = (float)$sessData['car_capacity'];
            } elseif (isset($confData['config']['car_capacity'])) {
                $carCapacity = parseNumericConfigValue($confData['config']['car_capacity'], 72.0);
            } else {
                $carCapacity = 72.0;
            }
            if ($carCapacity > 0) {
                $m = (int)date('n');
                $isWinter = ($m >= 11 || $m <= 3);
                $profileConsumption = (float)($veh['consumption'] ?? $veh['consumption_kwh_100km'] ?? $veh['avg_consumption'] ?? 0);
                $consumption = $profileConsumption > 0 ? $profileConsumption : ($isWinter ? 23.5 : 16.0); // kWh/100km
                $veh['range_km'] = round((($veh['soc'] / 100.0) * $carCapacity / $consumption) * 100);
            }
        }
    }
    unset($veh);

    $dedupedVehicles = [];
    $vehicleIndexById = [];
    $vehicleIndexByName = [];
    $vehicleIdentityBoundByIndex = [];
    foreach (ambiguousSavedCarNameKeys($savedCars ?? []) as $ambiguousNameKey) {
        // Die bekannte Profilmehrdeutigkeit gilt bereits vor dem ersten
        // Live-Datensatz. So kann dessen Reihenfolge kein Profil erraten.
        $vehicleIndexByName[$ambiguousNameKey] = null;
    }
    $legacyConfigPath = !empty($paths['valid']) && !empty($paths['install_path'])
        ? rtrim((string)$paths['install_path'], '/') . '/e3dc.config.txt'
        : null;
    $vehicleSocCloudFreshnessS = vehicleSocResolvedCloudFreshnessSeconds(
        $wallboxConfig ?? [],
        '/var/www/html/data/e3dc_v4.json',
        $legacyConfigPath
    );
    foreach ($data['vehicles'] as $veh) {
        if (!is_array($veh)) continue;
        $vehicleWbSlot = (int)($veh['wb_slot'] ?? 0);
        if ($vehicleWbSlot === 1 || $vehicleWbSlot === 2) {
            $slotConnected = wallboxSlotLooksConnected($data, $vehicleWbSlot);
            if (!$slotConnected) {
                $veh['is_plugged_in'] = false;
                $veh['is_charging'] = false;
                unset($veh['time_to_target_mins']);
            }
        }
        $profileId = savedCarProfileIdForVehicleRecord($veh, $savedCars ?? []);
        if ($profileId !== null && $profileId !== '') {
            $veh['profile_id'] = $profileId;
            $veh['id'] = $profileId;
            foreach (($savedCars ?? []) as $profileCar) {
                if (($profileCar['id'] ?? null) !== $profileId) continue;
                if (!empty($profileCar['name'])) $veh['name'] = $profileCar['name'];
                foreach ([
                    'capacity' => 'capacity',
                    'capacity_kwh' => 'capacity',
                    'power' => 'power',
                    'charge_power' => 'power',
                    'target_soc' => 'target_soc',
                    'max_soc' => 'max_soc',
                    'cloud_vehicle_id' => 'cloud_vehicle_id',
                    'vehicle_id' => 'vehicle_id',
                ] as $vehKey => $profileKey) {
                    if (!empty($profileCar[$profileKey])) $veh[$vehKey] = $profileCar[$profileKey];
                }
                break;
            }
        }
        $id = trim((string)($veh['id'] ?? ''));
        if ($id === '') {
            $id = 'vehicle_' . substr(md5(($veh['name'] ?? 'unknown') . '|' . ($veh['wb_slot'] ?? '') . '|' . count($dedupedVehicles)), 0, 12);
            $veh['id'] = $id;
        }
        $nameKey = normalizeVehicleMergeKey($veh['name'] ?? '');
        $profileKey = !empty($veh['profile_id']) ? ('profile:' . $veh['profile_id']) : '';
        $currentIdentityBound = vehicleRecordHasBoundDedupeIdentity($veh);

        $mergeIndex = null;
        if ($profileKey !== '' && isset($vehicleIndexById[$profileKey])) {
            $mergeIndex = $vehicleIndexById[$profileKey];
        } elseif ($id !== '' && isset($vehicleIndexById[$id])) {
            $mergeIndex = $vehicleIndexById[$id];
        } else {
            $mergeIndex = vehicleNameFallbackMergeIndex(
                $nameKey,
                $vehicleIndexByName,
                $vehicleIdentityBoundByIndex,
                $currentIdentityBound
            );
        }

        if ($mergeIndex !== null) {
            $dedupedVehicles[$mergeIndex] = mergeVehicleRecords(
                $dedupedVehicles[$mergeIndex],
                $veh,
                $vehicleSocCloudFreshnessS
            );
        } else {
            $mergeIndex = count($dedupedVehicles);
            $dedupedVehicles[] = $veh;
        }

        if ($profileKey !== '') $vehicleIndexById[$profileKey] = $mergeIndex;
        if ($id !== '') $vehicleIndexById[$id] = $mergeIndex;
        $vehicleIdentityBoundByIndex[$mergeIndex] = $currentIdentityBound
            || (($vehicleIdentityBoundByIndex[$mergeIndex] ?? false) === true);
        $vehicleIndexByName = registerVehicleNameMergeIndex(
            $vehicleIndexByName,
            $nameKey,
            $mergeIndex
        );
    }
    foreach ($dedupedVehicles as &$dedupedVehicle) {
        sanitizeWallboxVehicleSoc($dedupedVehicle, $vehicleSocCloudFreshnessS);
    }
    unset($dedupedVehicle);
    $data['vehicles'] = array_values($dedupedVehicles);
    $data['vehicle_soc'] = [];
    foreach ($data['vehicles'] as $vehicle) {
        if (!is_array($vehicle) || !isset($vehicle['soc_meta']) || !is_array($vehicle['soc_meta'])) continue;
        $data['vehicle_soc'][] = [
            'name' => $vehicle['name'] ?? '',
            'wb_slot' => isset($vehicle['wb_slot']) ? (int)$vehicle['wb_slot'] : null,
            'value' => $vehicle['soc_meta']['value'],
            'profile_id' => $vehicle['soc_meta']['profile_id'],
            'original_source' => $vehicle['soc_meta']['original_source'],
            'source_ts' => $vehicle['soc_meta']['source_ts'],
            'age_s' => $vehicle['soc_meta']['age_s'],
            'class' => $vehicle['soc_meta']['class'],
            'transport_class' => $vehicle['soc_meta']['transport_class'],
            'stale' => $vehicle['soc_meta']['stale'],
            'rule_usable' => $vehicle['soc_meta']['rule_usable'],
        ];
    }
    saveVehicleSocDisplayCache($vehicleSocDisplayCacheFile, $data['vehicles'], $vehicleSocDisplayCache);
}


// --- NEU: ML-Prognose einmischen ---
$mlFile = '/var/www/html/ramdisk/ml_prediction.json';
if (file_exists($mlFile)) {
    $mlData = @json_decode(file_get_contents($mlFile), true);
    if ($mlData) {
        $mlMaxAgeS = 36 * 3600;
        $mlFileMtime = @filemtime($mlFile);
        $mlFileAgeS = $mlFileMtime ? max(0, time() - $mlFileMtime) : null;
        $mlPayloadTs = $mlData['ts'] ?? null;
        $mlPayloadAgeS = null;
        if (is_string($mlPayloadTs) && trim($mlPayloadTs) !== '') {
            $parsedTs = strtotime($mlPayloadTs);
            if ($parsedTs !== false) {
                $mlPayloadAgeS = max(0, time() - $parsedTs);
                $data['ml_prediction_ts'] = $mlPayloadTs;
            }
        }
        $mlAgeS = $mlPayloadAgeS ?? $mlFileAgeS;
        $mlStale = $mlAgeS !== null && $mlAgeS > $mlMaxAgeS;
        if ($mlAgeS !== null) $data['ml_prediction_age_s'] = $mlAgeS;
        $data['ml_prediction_stale'] = $mlStale;
        if (!$mlStale) {
            $data['ml_home_kwh'] = $mlData['home_kwh'] ?? null;
            $data['ml_wp_kwh'] = $mlData['wp_kwh'] ?? null;
            $data['ml_climate_kwh'] = $mlData['climate_kwh'] ?? null;
        } else {
            $data['ml_home_kwh'] = null;
            $data['ml_wp_kwh'] = null;
            $data['ml_climate_kwh'] = null;
        }
    }
}

// --- NEU: Daily Min/Max Peaks (RAM-Disk Track) ---
if ($validData) {
    $peaksFile = '/var/www/html/ramdisk/daily_peaks.json';
    $today = date('Y-m-d');

    $peaks = [
        'date' => $today,
        'pv_max' => 0,
        'grid_max_in' => 0,
        'grid_max_out' => 0,
        'home_max' => 0,
        'home_min' => 99999,
        'bat_max_in' => 0,
        'bat_max_out' => 0,
        'wb_max' => 0,
        'wb2_max' => 0,
        'wp_max' => 0
    ];

    if (file_exists($peaksFile)) {
        $savedPeaks = @json_decode(file_get_contents($peaksFile), true);
        if ($savedPeaks && isset($savedPeaks['date']) && $savedPeaks['date'] === $today) {
            $peaks = array_merge($peaks, $savedPeaks);
        }
    }

    $changed = false;

    // Check PV
    $pv = max(0, $data['pv'] ?? 0);
    if ($pv > $peaks['pv_max']) { $peaks['pv_max'] = $pv; $changed = true; }

    // Check Grid
    $grid = $data['grid'] ?? 0;
    if ($grid > 0 && $grid > $peaks['grid_max_in']) { $peaks['grid_max_in'] = $grid; $changed = true; }
    if ($grid < 0 && abs($grid) > $peaks['grid_max_out']) { $peaks['grid_max_out'] = abs($grid); $changed = true; }

    // Check Home (Pure Hausverbrauch ohne WP/WB)
    $home = isset($realHome) ? max(0, $realHome) : max(0, $data['home'] ?? 0);
    if ($home > 0) {
        if ($home > $peaks['home_max']) { $peaks['home_max'] = $home; $changed = true; }
        if ($home < $peaks['home_min']) { $peaks['home_min'] = $home; $changed = true; }
    }

    // Check Battery
    $bat = $data['bat'] ?? 0;
    if ($bat > 0 && $bat > $peaks['bat_max_out']) { $peaks['bat_max_out'] = $bat; $changed = true; }
    if ($bat < 0 && abs($bat) > $peaks['bat_max_in']) { $peaks['bat_max_in'] = abs($bat); $changed = true; }

    // Check Wallbox 1
    $wb = $data['wb'] ?? 0;
    if ($wb > 0 && $wb > ($peaks['wb_max'] ?? 0)) { $peaks['wb_max'] = $wb; $changed = true; }

    // Check Wallbox 2
    $wb2 = $data['wb2'] ?? 0;
    if ($wb2 > 0 && $wb2 > ($peaks['wb2_max'] ?? 0)) { $peaks['wb2_max'] = $wb2; $changed = true; }

    // Check Wärmepumpe
    $wp = $data['wp'] ?? 0;
    if ($wp > 0 && $wp > $peaks['wp_max']) { $peaks['wp_max'] = $wp; $changed = true; }

    // Min-Fallback abfangen
    if ($peaks['home_min'] == 99999) $peaks['home_min'] = 0;

    if ($changed) {
        @file_put_contents($peaksFile, json_encode($peaks));
        @chmod($peaksFile, 0664);
    }
    $data['peaks'] = $peaks;
    if (isset($data['wb2']) && $data['wb2'] == 0 && empty($data['is_external_wb2'])) {
        $cfgWb2 = $confData['config']['wb2_ip'] ?? '';
        $cfgShellyWb2 = $confData['config']['shelly_wb2_ip'] ?? '';
        $cfgNativeWb2 = $confData['config']['wb_native_ip2'] ?? '';
        $cfgNativeWb2Type = $confData['config']['wb_native_type2'] ?? '';
        if ((empty($cfgWb2) || $cfgWb2 === '0.0.0.0')
            && (empty($cfgShellyWb2) || $cfgShellyWb2 === '0.0.0.0')
            && (empty($cfgNativeWb2) || $cfgNativeWb2 === '0.0.0.0')
            && (empty($cfgNativeWb2Type) || $cfgNativeWb2Type === 'none')) {
            unset($data['wb2']);
        }
    }
}

// --- Ladekurve: Meilensteine aus storage_manager_state.json ---
// Wird vom storage_manager (V4) befüllt, enthält Ladestart/Peak/Freilauf mit SOC-Schaetzung.
$storageDisplayDayStart = null;
$storageDisplayDayEnd = null;
$storageDisplayDayLabel = 'Heute';
$storageAuthoritativeSnapshotLoaded = false;
$storStateFile = $isShadowMode
    ? '/var/www/html/ramdisk/shadow_master_storage_manager_state.json'
    : '/var/www/html/ramdisk/storage_manager_state.json';
if (
    (!$isShadowMode || $shadowStorageProjectionAllowed)
    && file_exists($storStateFile)
    && (time() - filemtime($storStateFile) < 120)
) {
    $storState = @json_decode(file_get_contents($storStateFile), true);
    if ($storState) {
        $storageAuthoritativeSnapshotLoaded = !empty($storState['state']);
        // Ladekurve-Meilensteine (von Python SOC-Trajektorie berechnet)
        if (!empty($storState['ladekurve'])) {
            $data['ladekurve'] = $storState['ladekurve'];
            if (!empty($storState['ladekurve']['day_start_ts'])) {
                $storageDisplayDayStart = (int)($storState['ladekurve']['day_start_ts'] / 1000);
                $storageDisplayDayEnd = $storageDisplayDayStart + 86400;
            }
            if (!empty($storState['ladekurve']['day_label'])) {
                $storageDisplayDayLabel = $storState['ladekurve']['day_label'];
            }
        }
        // Betriebsphase des Speicher-Managers ("Freilauf", "Sanftes Laden", ...)
        if (!empty($storState['phase'])) {
            $data['storage_phase'] = $storState['phase'];
        }
        if (!empty($storState['state'])) {
            $data['storage_state'] = $storState['state'];
        }
        if (!empty($storState['manager_title'])) {
            $data['storage_manager_title'] = $storState['manager_title'];
        }
        if (!empty($storState['state_label'])) {
            $data['storage_state_label'] = $storState['state_label'];
        }
        if (!empty($storState['control_owner'])) {
            $data['storage_control_owner'] = $storState['control_owner'];
        }
        if (!empty($storState['control_owner_label'])) {
            $data['storage_control_owner_label'] = $storState['control_owner_label'];
        }
        $chargeAcceptanceDiagnostic = $storState['charge_acceptance_diagnostic'] ?? null;
        if (is_array($chargeAcceptanceDiagnostic)) {
            $data['storage_charge_acceptance_diagnostic'] = $chargeAcceptanceDiagnostic;
            $data['storage_charge_acceptance_text'] = !empty($chargeAcceptanceDiagnostic['active'])
                && is_string($chargeAcceptanceDiagnostic['display_text'] ?? null)
                ? $chargeAcceptanceDiagnostic['display_text']
                : null;
        }
        foreach ([
            'iFc_w' => 'storage_ifc_w',
            'iMinLade_w' => 'storage_imin_w',
            'storage_charge_request_w' => 'storage_charge_request_w',
            'wallbox_curve_reserve_w' => 'wallbox_curve_reserve_w',
            'wallbox_curve_reserve_target_w' => 'wallbox_curve_reserve_target_w',
            'wallbox_curve_reserve_step_w' => 'wallbox_curve_reserve_step_w',
            'curve_control_soc' => 'storage_curve_control_soc',
            'curve_control_raw_soc' => 'storage_curve_raw_soc',
            'tl_soc_now' => 'storage_curve_soc_now',
            'tl_soc_target' => 'storage_curve_soc_target',
            'tl_ts_target' => 'storage_curve_target_ts',
            'curve_gap_pct' => 'storage_curve_gap_pct',
            'curve_gap_catchup_w' => 'storage_curve_catchup_w',
            'curve_gap_catchup_cap_w' => 'storage_curve_catchup_cap_w',
            'curve_gap_catchup_factor' => 'storage_curve_catchup_factor',
            'curve_gap_catchup_min_w' => 'storage_curve_catchup_min_w',
            'curve_gap_catchup_taper_pct' => 'storage_curve_catchup_taper_pct',
            'curve_need_raw_w' => 'storage_curve_need_raw_w',
            'lookahead_need_w' => 'storage_lookahead_need_w',
            'curve_hard_anchor_need_w' => 'storage_curve_hard_anchor_need_w',
            'curve_hard_anchor_gap_pct' => 'storage_curve_hard_anchor_gap_pct',
            'curve_frame_lift_w' => 'storage_curve_frame_lift_w',
            'curve_frame_lift_desired_w' => 'storage_curve_frame_lift_desired_w',
            'curve_frame_lift_actual_w' => 'storage_curve_frame_lift_actual_w',
            'curve_frame_lift_shortfall_w' => 'storage_curve_frame_lift_shortfall_w',
            'mode' => 'storage_mode',
            'val' => 'storage_val_w',
            'max_charge_w' => 'storage_max_charge_w',
            'max_discharge_w' => 'storage_max_discharge_w',
            'abregel_charge_req_w' => 'storage_abregel_req_w',
            'abregel_grid_pressure_w' => 'storage_abregel_grid_pressure_w',
            'abregel_physical_pressure_w' => 'storage_abregel_physical_pressure_w',
            'abregel_inverter_pressure_w' => 'storage_abregel_inverter_pressure_w',
            'abregel_grid_error_w' => 'storage_abregel_grid_error_w',
            'abregel_target_w' => 'storage_abregel_target_w',
            'abregel_release_w' => 'storage_abregel_release_w',
            'abregel_rscp_limit_w' => 'storage_abregel_rscp_limit_w',
            'adaptive_soc_floor' => 'storage_adaptive_soc_floor',
            'adaptive_soc_ceiling' => 'storage_adaptive_soc_ceiling',
            'adaptive_headroom_required_wh' => 'storage_adaptive_headroom_required_wh',
            'adaptive_headroom_available_wh' => 'storage_adaptive_headroom_available_wh',
            'curtailment_pressure_wh' => 'storage_curtailment_pressure_wh',
            'curtailment_unavoidable_wh' => 'storage_curtailment_unavoidable_wh',
            'latest_charge_start_ts' => 'storage_latest_charge_start_ts',
            'headroom_discharge_today_wh' => 'storage_headroom_discharge_today_wh',
            'headroom_discharge_daily_limit_wh' => 'storage_headroom_discharge_daily_limit_wh',
            'headroom_discharge_daily_remaining_wh' => 'storage_headroom_discharge_daily_remaining_wh',
            'headroom_discharge_daily_limit_pct' => 'storage_headroom_discharge_daily_limit_pct',
            'headroom_discharge_cooldown_s' => 'storage_headroom_discharge_cooldown_s',
            'headroom_discharge_cooldown_remaining_s' => 'storage_headroom_discharge_cooldown_remaining_s',
            'headroom_discharge_target_curve_soc' => 'storage_headroom_discharge_target_curve_soc',
            'headroom_discharge_target_plateau_margin_pct' => 'storage_headroom_discharge_target_plateau_margin_pct',
            'evening_shortfall_wh' => 'storage_evening_shortfall_wh',
        ] as $srcKey => $dstKey) {
            if (isset($storState[$srcKey]) && is_numeric($storState[$srcKey])) {
                $data[$dstKey] = $storState[$srcKey] + 0;
            }
        }
        if (isset($storState['abregel_active'])) {
            $data['storage_abregel_active'] = (bool)$storState['abregel_active'];
        }
        if (!empty($storState['abregel_source'])) {
            $data['storage_abregel_source'] = (string)$storState['abregel_source'];
        }
        if (isset($storState['adaptive_curve_active'])) {
            $data['storage_adaptive_curve_active'] = (bool)$storState['adaptive_curve_active'];
        }
        if (!empty($storState['adaptive_curve_relation'])) {
            $data['storage_adaptive_curve_relation'] = (string)$storState['adaptive_curve_relation'];
        }
        if (isset($storState['adaptive_latest_charge_due'])) {
            $data['storage_adaptive_latest_charge_due'] = (bool)$storState['adaptive_latest_charge_due'];
        }
        if (!empty($storState['headroom_discharge_day'])) {
            $data['storage_headroom_discharge_day'] = (string)$storState['headroom_discharge_day'];
        }
        if (!empty($storState['headroom_discharge_blocked_reason'])) {
            $data['storage_headroom_discharge_blocked_reason'] = (string)$storState['headroom_discharge_blocked_reason'];
        }
        foreach ([
            'headroom_discharge_daily_blocked' => 'storage_headroom_discharge_daily_blocked',
            'headroom_discharge_cooldown_active' => 'storage_headroom_discharge_cooldown_active',
            'headroom_discharge_target_plateau_reached' => 'storage_headroom_discharge_target_plateau_reached',
        ] as $srcKey => $dstKey) {
            if (isset($storState[$srcKey])) {
                $data[$dstKey] = (bool)$storState[$srcKey];
            }
        }
        if (!empty($storState['cheap_grid_charge']) && is_array($storState['cheap_grid_charge'])) {
            $data['cheap_grid_charge'] = $storState['cheap_grid_charge'];
        }
        if (!empty($storState['direct_marketing_monitor']) && is_array($storState['direct_marketing_monitor'])) {
            $data['direct_marketing_monitor'] = $storState['direct_marketing_monitor'];
        }
        if (!empty($storState['direct_marketing_daily_report']) && is_array($storState['direct_marketing_daily_report'])) {
            $data['direct_marketing_daily_report'] = $storState['direct_marketing_daily_report'];
        }
        $auxShellyState = $storState['direct_marketing_aux_inverter_shelly'] ?? null;
        if (is_array($auxShellyState)) {
            $data['direct_marketing_aux_inverter_shelly'] = $auxShellyState;
            $data['pv_external_capable'] = $data['pv_external_capable'] || !empty($auxShellyState['ip_configured']);
        }
        if (!empty($storState['auto_limit']) && is_array($storState['auto_limit'])) {
            $data['storage_auto_limit'] = $storState['auto_limit'];
        }
        // Sonnenzeiten (falls Energiefluss-Chart diese braucht)
        foreach (['t_sunrise', 't_noon', 't_pv_peak', 't_sunset', 'sunrise_h', 'noon_h', 'pv_peak_h', 'sunset_h'] as $k) {
            if (isset($storState[$k])) $data[$k] = $storState[$k];
        }
// Reason-Text für Tooltip/Diagnose
        if (!empty($storState['display_reason'])) {
            $data['storage_reason'] = $storState['display_reason'];
        } elseif (!empty($storState['reason'])) {
            $data['storage_reason'] = $storState['reason'];
        }
    }
}

// --- Direktvermarktung: Tagesauswertung aus Storage-Manager-Monitor ---
$directMarketingReportFile = '/var/www/html/ramdisk/direct_marketing_daily_report.json';
if (file_exists($directMarketingReportFile) && (time() - filemtime($directMarketingReportFile) < 90000)) {
    $directMarketingReport = @json_decode(file_get_contents($directMarketingReportFile), true);
    if (is_array($directMarketingReport)) {
        $data['direct_marketing_daily_report'] = $directMarketingReport;
    }
}

$directMarketingAuxShellyFiles = ['/var/www/html/ramdisk/direct_marketing_aux_inverter_shelly_state.json'];
foreach ($directMarketingAuxShellyFiles as $directMarketingAuxShellyFile) {
    if (file_exists($directMarketingAuxShellyFile) && (time() - filemtime($directMarketingAuxShellyFile) < 120)) {
        $directMarketingAuxShelly = @json_decode(file_get_contents($directMarketingAuxShellyFile), true);
        if (is_array($directMarketingAuxShelly)) {
            $data['direct_marketing_aux_inverter_shelly'] = $directMarketingAuxShelly;
            $data['pv_external_capable'] = $data['pv_external_capable'] || !empty($directMarketingAuxShelly['ip_configured']);
            break;
        }
    }
}

// --- Direktvermarktung: vorläufiger Marktwert-Solar-Monitor ---
$marketValueSolarFile = '/var/www/html/ramdisk/market_value_solar.json';
if (file_exists($marketValueSolarFile) && (time() - filemtime($marketValueSolarFile) < 172800)) {
    $marketValueSolar = @json_decode(file_get_contents($marketValueSolarFile), true);
    if (is_array($marketValueSolar)) {
        $data['market_value_solar'] = $marketValueSolar;
    }
}

// --- WB-Budget-Signal (wb_pv_budget.json) ---
$wbBudgetFile = $isShadowMode
    ? '/var/www/html/ramdisk/shadow_master_wb_pv_budget.json'
    : '/var/www/html/ramdisk/wb_pv_budget.json';
if (
    (!$isShadowMode || $shadowWallboxBudgetProjectionAllowed)
    && file_exists($wbBudgetFile)
    && (time() - filemtime($wbBudgetFile) < 30)
) {
    $wbBudget = @json_decode(file_get_contents($wbBudgetFile), true);
    if ($wbBudget) {
        $wbBudgetDiagnosticFile = $isShadowMode
            ? null
            : '/var/www/html/ramdisk/wb_pv_budget_diagnostics.json';
        if (
            is_string($wbBudgetDiagnosticFile)
            && file_exists($wbBudgetDiagnosticFile)
            && (time() - filemtime($wbBudgetDiagnosticFile) < 180)
        ) {
            $wbBudgetDiagnostic = @json_decode(file_get_contents($wbBudgetDiagnosticFile), true);
            if (is_array($wbBudgetDiagnostic)) {
                // Die frische Kontrollfläche gewinnt bei gleichnamigen Feldern.
                $wbBudget = array_merge($wbBudgetDiagnostic, $wbBudget);
            }
        }
        $rawBudgetState = $wbBudget['state'] ?? 'unknown';
        $signalStates = ['reduce', 'stop', 'timeout', 'hold'];
        $data['wb_budget_w']       = (int)($wbBudget['budget_w'] ?? 0);
        $data['wb_budget_amp_1ph'] = (int)($wbBudget['budget_amp_1ph'] ?? 0);
        $data['wb_budget_amp_3ph'] = (int)($wbBudget['budget_amp_3ph'] ?? 0);
        $data['wb_budget_state']   = in_array($rawBudgetState, $signalStates, true) ? $rawBudgetState : 'run';
        $data['wb_budget_storage_state'] = $wbBudget['storage_state'] ?? 'unknown';
        // Speicherzustand und Owner stammen aus genau einer autoritativen
        // Oberfläche. Das Verbraucherbudget wird nach dem State-Snapshot
        // geschrieben und kann in einem benachbarten Zyklus liegen. Würde es
        // diese Felder erneut überschreiben, könnte die UI zwischen E3DC/AUTO
        // und Storage Manager/DV springen, obwohl kein Owner gewechselt hat.
        // Nur wenn der State-Snapshot fehlt, dient das Budget als Fallback.
        if (!$storageAuthoritativeSnapshotLoaded) {
            $data['storage_state'] = $wbBudget['storage_state'] ?? 'unknown';
            $data['storage_manager_title'] = $wbBudget['manager_title'] ?? ($data['storage_manager_title'] ?? '');
            $data['storage_state_label'] = $wbBudget['state_label'] ?? ($data['storage_state_label'] ?? '');
            $data['storage_control_owner'] = $wbBudget['control_owner'] ?? ($data['storage_control_owner'] ?? '');
            $data['storage_control_owner_label'] = $wbBudget['control_owner_label'] ?? ($data['storage_control_owner_label'] ?? '');
            if (!empty($wbBudget['display_reason'])) {
                $data['storage_reason'] = $wbBudget['display_reason'];
            }
        }
        $data['wb_budget_reason']  = $wbBudget['reason'] ?? '';
        $es = $wbBudget['energy_score'] ?? [];
        $data['free_for_limbs_w']   = (int)($es['free_for_limbs_w'] ?? 0);
        $data['free_for_limbs_raw_w'] = (int)($es['free_for_limbs_raw_w'] ?? $data['free_for_limbs_w']);
        $consumerAlloc = $wbBudget['consumer_allocations'] ?? ($es['consumer_allocations'] ?? []);
        if (is_array($consumerAlloc)) {
            $data['consumer_allocations'] = $consumerAlloc;
            $data['heatpump_budget_w'] = (int)($consumerAlloc['heatpump'] ?? 0);
            $data['wallbox_budget_w'] = (int)($consumerAlloc['wallbox'] ?? $data['wb_budget_w']);
            $data['heater_budget_w'] = (int)($consumerAlloc['heater'] ?? 0);
            if (($data['heat_manager_label'] ?? '') === 'Beobachtet' && (int)$data['heatpump_budget_w'] > 0) {
                $data['heat_manager_label'] = 'Budget bereit';
                $data['heat_manager_owner_key'] = 'storage_budget_heatpump';
                $data['heat_manager_owner_label'] = 'Budget bereit';
                $data['heat_manager_owner_kind'] = 'budget_ready';
                $data['heat_manager_owner_reason'] = 'Storage Manager bietet Wärmebudget an; die Wärmepumpe nimmt aktuell noch keine Leistung auf.';
                $data['heat_manager_reason'] = $data['heat_manager_owner_reason'] . ' | Budget ' . (int)$data['heatpump_budget_w'] . ' W';
            }
        }
        $data['heatpump_start_request_w'] = (int)($wbBudget['heatpump_start_request_w'] ?? 0);
        $data['authorized_heatpump_budget_w'] = (int)($wbBudget['authorized_heatpump_budget_w'] ?? 0);
        $data['heatpump_start_state'] = (string)($wbBudget['heatpump_start_state'] ?? 'idle');
        $data['heatpump_start_reason_code'] = (string)($wbBudget['heatpump_start_reason_code'] ?? 'none');
        $data['released_budget_receiver'] = (string)($wbBudget['released_budget_receiver'] ?? 'none');
        $data['consumer_priority_order'] = $wbBudget['consumer_priority_order'] ?? ($es['consumer_priority_order'] ?? null);
        $data['consumer_priority_effective_order'] = $wbBudget['consumer_priority_effective_order'] ?? ($es['consumer_priority_effective_order'] ?? null);
        $data['bat_charge_req_w']   = (int)($es['bat_charge_request_w'] ?? 0);
        $data['storage_charge_request_w'] = (int)($wbBudget['storage_charge_request_w'] ?? ($es['bat_charge_request_w'] ?? ($data['storage_charge_request_w'] ?? 0)));
        $data['wallbox_curve_reserve_w'] = (int)($wbBudget['wallbox_curve_reserve_w'] ?? ($data['wallbox_curve_reserve_w'] ?? 0));
        $data['wallbox_curve_reserve_target_w'] = (int)($wbBudget['wallbox_curve_reserve_target_w'] ?? ($data['wallbox_curve_reserve_target_w'] ?? 0));
        $data['wallbox_curve_reserve_step_w'] = (int)($wbBudget['wallbox_curve_reserve_step_w'] ?? ($data['wallbox_curve_reserve_step_w'] ?? 0));
        if (isset($es['abregel_charge_request_w'])) {
            $data['storage_abregel_req_w'] = (int)$es['abregel_charge_request_w'];
        } elseif (isset($wbBudget['abregel_charge_req_w'])) {
            $data['storage_abregel_req_w'] = (int)$wbBudget['abregel_charge_req_w'];
        }
        foreach ([
            'abregel_grid_pressure_w' => 'storage_abregel_grid_pressure_w',
            'abregel_physical_pressure_w' => 'storage_abregel_physical_pressure_w',
            'abregel_inverter_pressure_w' => 'storage_abregel_inverter_pressure_w',
            'abregel_grid_error_w' => 'storage_abregel_grid_error_w',
            'abregel_target_w' => 'storage_abregel_target_w',
            'abregel_release_w' => 'storage_abregel_release_w',
            'abregel_rscp_limit_w' => 'storage_abregel_rscp_limit_w',
            'curve_gap_pct' => 'storage_curve_gap_pct',
            'curve_gap_catchup_w' => 'storage_curve_catchup_w',
            'curve_gap_catchup_cap_w' => 'storage_curve_catchup_cap_w',
            'curve_gap_catchup_factor' => 'storage_curve_catchup_factor',
            'curve_gap_catchup_min_w' => 'storage_curve_catchup_min_w',
            'curve_gap_catchup_taper_pct' => 'storage_curve_catchup_taper_pct',
            'curve_need_raw_w' => 'storage_curve_need_raw_w',
            'lookahead_need_w' => 'storage_lookahead_need_w',
            'curve_hard_anchor_need_w' => 'storage_curve_hard_anchor_need_w',
            'curve_hard_anchor_gap_pct' => 'storage_curve_hard_anchor_gap_pct',
            'curve_frame_lift_w' => 'storage_curve_frame_lift_w',
            'curve_frame_lift_desired_w' => 'storage_curve_frame_lift_desired_w',
            'curve_frame_lift_actual_w' => 'storage_curve_frame_lift_actual_w',
            'curve_frame_lift_shortfall_w' => 'storage_curve_frame_lift_shortfall_w',
            'wallbox_curve_reserve_w' => 'wallbox_curve_reserve_w',
            'wallbox_curve_reserve_target_w' => 'wallbox_curve_reserve_target_w',
            'wallbox_curve_reserve_step_w' => 'wallbox_curve_reserve_step_w',
            'adaptive_soc_floor' => 'storage_adaptive_soc_floor',
            'adaptive_soc_ceiling' => 'storage_adaptive_soc_ceiling',
            'adaptive_headroom_required_wh' => 'storage_adaptive_headroom_required_wh',
            'adaptive_headroom_available_wh' => 'storage_adaptive_headroom_available_wh',
            'curtailment_pressure_wh' => 'storage_curtailment_pressure_wh',
            'curtailment_unavoidable_wh' => 'storage_curtailment_unavoidable_wh',
            'latest_charge_start_ts' => 'storage_latest_charge_start_ts',
            'headroom_discharge_today_wh' => 'storage_headroom_discharge_today_wh',
            'headroom_discharge_daily_limit_wh' => 'storage_headroom_discharge_daily_limit_wh',
            'headroom_discharge_daily_remaining_wh' => 'storage_headroom_discharge_daily_remaining_wh',
            'headroom_discharge_daily_limit_pct' => 'storage_headroom_discharge_daily_limit_pct',
            'headroom_discharge_cooldown_s' => 'storage_headroom_discharge_cooldown_s',
            'headroom_discharge_cooldown_remaining_s' => 'storage_headroom_discharge_cooldown_remaining_s',
            'headroom_discharge_target_curve_soc' => 'storage_headroom_discharge_target_curve_soc',
            'headroom_discharge_target_plateau_margin_pct' => 'storage_headroom_discharge_target_plateau_margin_pct',
            'evening_shortfall_wh' => 'storage_evening_shortfall_wh',
        ] as $srcKey => $dstKey) {
            if (isset($wbBudget[$srcKey]) && is_numeric($wbBudget[$srcKey])) {
                $data[$dstKey] = $wbBudget[$srcKey] + 0;
            } elseif (isset($es[$srcKey]) && is_numeric($es[$srcKey])) {
                $data[$dstKey] = $es[$srcKey] + 0;
            }
        }
        if (isset($wbBudget['abregel_active'])) {
            $data['storage_abregel_active'] = (bool)$wbBudget['abregel_active'];
        }
        if (!empty($wbBudget['abregel_source'])) {
            $data['storage_abregel_source'] = (string)$wbBudget['abregel_source'];
        }
        if (!empty($wbBudget['direct_marketing_monitor']) && is_array($wbBudget['direct_marketing_monitor'])) {
            $data['direct_marketing_monitor'] = $wbBudget['direct_marketing_monitor'];
        }
        if (!empty($wbBudget['direct_marketing_daily_report']) && is_array($wbBudget['direct_marketing_daily_report'])) {
            $data['direct_marketing_daily_report'] = $wbBudget['direct_marketing_daily_report'];
        }
        if (isset($wbBudget['adaptive_curve_active'])) {
            $data['storage_adaptive_curve_active'] = (bool)$wbBudget['adaptive_curve_active'];
        }
        if (!empty($wbBudget['adaptive_curve_relation'])) {
            $data['storage_adaptive_curve_relation'] = (string)$wbBudget['adaptive_curve_relation'];
        }
        if (isset($wbBudget['adaptive_latest_charge_due'])) {
            $data['storage_adaptive_latest_charge_due'] = (bool)$wbBudget['adaptive_latest_charge_due'];
        }
        if (!empty($wbBudget['headroom_discharge_day'])) {
            $data['storage_headroom_discharge_day'] = (string)$wbBudget['headroom_discharge_day'];
        }
        if (!empty($wbBudget['headroom_discharge_blocked_reason'])) {
            $data['storage_headroom_discharge_blocked_reason'] = (string)$wbBudget['headroom_discharge_blocked_reason'];
        }
        foreach ([
            'headroom_discharge_daily_blocked' => 'storage_headroom_discharge_daily_blocked',
            'headroom_discharge_cooldown_active' => 'storage_headroom_discharge_cooldown_active',
            'headroom_discharge_target_plateau_reached' => 'storage_headroom_discharge_target_plateau_reached',
        ] as $srcKey => $dstKey) {
            if (isset($wbBudget[$srcKey])) {
                $data[$dstKey] = (bool)$wbBudget[$srcKey];
            }
        }
        $data['pv_surplus_w']       = (int)($es['pv_surplus_w'] ?? 0);
        $data['wb_budget_age_s']   = max(0, time() - (int)($wbBudget['ts'] ?? 0));
    }
} else {
    $data['wb_budget_state']  = 'timeout';
    $data['wb_budget_reason'] = 'Kein Budget-Signal';
    $data['wb_budget_age_s']  = 999;
}

// --- Ladekurve und Dispatchprojektion aus genau einer storage_plan-plan_id ---
// PHP projiziert den kanonischen Slotvertrag und simuliert keine zweite
// Batterie-, SoC-, Direktvermarktungs- oder Marktentscheidung.
$storagePlanFile = '/var/www/html/ramdisk/storage_plan.json';
if (file_exists($storagePlanFile) && (time() - filemtime($storagePlanFile) < 900)) {
    $storPlanRawJson = @file_get_contents($storagePlanFile);
    $storPlan = is_string($storPlanRawJson) ? @json_decode($storPlanRawJson, true) : null;
    if ($storPlan) {
        $storageActionProjectionFile = '/var/www/html/ramdisk/storage_plan_action_projection.json';
        $storageActionProjectionArtifact = liveReadStoragePlanActionProjectionArtifact($storageActionProjectionFile);
        $storageDispatchRuntimeFile = '/var/www/html/ramdisk/storage_dispatch_runtime.json';
        $storageDispatchRuntime = is_readable($storageDispatchRuntimeFile)
            ? @json_decode(file_get_contents($storageDispatchRuntimeFile), true)
            : null;
        $data['storage_dispatch_runtime'] = is_array($storageDispatchRuntime)
            ? $storageDispatchRuntime
            : [
                'schema_version' => 'storage_dispatch_runtime_v1',
                'plan_id' => $storPlan['plan_id'] ?? null,
                'slot_id' => null,
                'plan_valid' => false,
                'commands_allowed' => false,
                'owner' => 'storage_manager',
                'block_reason_code' => 'RUNTIME_OVERLAY_MISSING',
                'charge_budget_w' => 0,
                'export_budget_w' => 0,
            ];
        $targetCurveMeta = (isset($storPlan['target_curve_meta']) && is_array($storPlan['target_curve_meta']))
            ? $storPlan['target_curve_meta']
            : [];
        // Nach PV-Ende plant der Simulator bereits den naechsten Tag. Die
        // Dashboard-Kurve muss diesem Plan-Tag folgen, sonst filtert sie
        // faelschlich "Heute" und die Ladekurve verschwindet im Frontend.
        if (!empty($targetCurveMeta['curve_day_start_ts'])) {
            $storageDisplayDayStart = (int)((float)$targetCurveMeta['curve_day_start_ts'] / 1000);
            $storageDisplayDayEnd = $storageDisplayDayStart + 86400;
            if (!empty($targetCurveMeta['curve_day_label'])) {
                $storageDisplayDayLabel = (string)$targetCurveMeta['curve_day_label'];
            }
        } elseif (!empty($storPlan['target_timeline'][0]['ts'])) {
            $firstCurveTs = (int)((float)$storPlan['target_timeline'][0]['ts'] / 1000);
            $storageDisplayDayStart = strtotime(date('Y-m-d', $firstCurveTs) . ' 00:00:00');
            $storageDisplayDayEnd = $storageDisplayDayStart + 86400;
        }
        $today0 = $storageDisplayDayStart ?: mktime(0, 0, 0);
        $today1 = $storageDisplayDayEnd ?: ($today0 + 86400);
        $targetCurve = [];
        $socMinCurve = [];
        $socCeilingCurve = [];
        $simCurve    = [];
        $curveAnchors = [];
        $directMarketingForCurve = (isset($storPlan['direct_marketing']) && is_array($storPlan['direct_marketing']))
            ? $storPlan['direct_marketing']
            : null;
        $canonicalPlan = (($storPlan['schema_version'] ?? '') === 'storage_dispatch_plan_v1')
            && preg_match('/^sha256:[0-9a-f]{64}$/', (string)($storPlan['plan_id'] ?? '')) === 1
            && isset($storPlan['slots'])
            && is_array($storPlan['slots']);
        $runtime = $data['storage_dispatch_runtime'];
        $effectiveStoragePlan = is_array($runtime['effective_storage_plan'] ?? null)
            ? $runtime['effective_storage_plan']
            : [];
        $effectiveBinding = is_array($effectiveStoragePlan['binding'] ?? null)
            ? $effectiveStoragePlan['binding']
            : [];
        $effectiveLifecycle = is_array($effectiveStoragePlan['lifecycle'] ?? null)
            ? $effectiveStoragePlan['lifecycle']
            : [];
        $runtimeLifecycle = is_array($runtime['requested'] ?? null) ? $runtime['requested'] : [];
        $runtimePhase5 = is_array($runtime['phase5'] ?? null) ? $runtime['phase5'] : [];
        $runtimePhase5Lifecycle = is_array($runtimePhase5['request_lifecycle'] ?? null)
            ? $runtimePhase5['request_lifecycle']
            : [];
        $runtimeTsMs = is_numeric($runtime['runtime_generated_at_ts_ms'] ?? null)
            ? (int)$runtime['runtime_generated_at_ts_ms']
            : 0;
        $nowMs = (int)round(microtime(true) * 1000);
        $runtimeAgeMs = $nowMs - $runtimeTsMs;
        $lifecycleBound = true;
        foreach (['selected', 'executable', 'commands_allowed'] as $key) {
            $lifecycleBound = $lifecycleBound
                && array_key_exists($key, $effectiveLifecycle)
                && ($effectiveLifecycle[$key] === ($runtime[$key] ?? null));
        }
        foreach (['requested', 'attempted', 'issued', 'confirmed', 'hardware_effect'] as $key) {
            $lifecycleBound = $lifecycleBound
                && array_key_exists($key, $effectiveLifecycle)
                && ($effectiveLifecycle[$key] === ($runtimeLifecycle[$key] ?? null));
        }
        foreach (['retained', 'retained_effect'] as $key) {
            $lifecycleBound = $lifecycleBound
                && array_key_exists($key, $effectiveLifecycle)
                && ($effectiveLifecycle[$key] === ($runtimePhase5Lifecycle[$key] ?? false));
        }
        $effectiveStatus = (string)($effectiveStoragePlan['status'] ?? '');
        $effectiveAction = strtoupper((string)($effectiveStoragePlan['effective_action'] ?? ''));
        $runtimeAction = strtoupper((string)($runtime['effective_action'] ?? ''));
        $knownEffectiveActions = ['CHARGE_BLOCK_WAIT', 'PV_STORE', 'DV_CURVE_CHARGE', 'ECONOMIC_EXPORT', 'HEADROOM_EXPORT', 'PASSIVE_NORMAL'];
        $effectConfirmed = ($effectiveLifecycle['effect_confirmed'] ?? null) === true;
        $selectionValid = $effectiveAction === 'PASSIVE_NORMAL'
            ? (($effectiveLifecycle['selected'] ?? null) === false
                && ($effectiveLifecycle['executable'] ?? null) === false
                && ($effectiveLifecycle['commands_allowed'] ?? null) === false)
            : (($effectiveLifecycle['selected'] ?? null) === true
                && ($effectiveLifecycle['executable'] ?? null) === true
                && ($effectiveLifecycle['commands_allowed'] ?? null) === true);
        $activeEffectLifecycle = ($effectiveLifecycle['requested'] ?? null) === true
            && ($effectiveLifecycle['hardware_effect'] ?? null) === true
            && (($effectiveLifecycle['issued'] ?? null) === true
                || ($effectiveLifecycle['retained'] ?? null) === true
                || ($effectiveLifecycle['retained_effect'] ?? null) === true)
            && (($effectiveLifecycle['confirmed'] ?? null) === true
                || in_array($effectiveAction, ['ECONOMIC_EXPORT', 'HEADROOM_EXPORT'], true));
        $passiveLifecycleClear = true;
        foreach (['requested', 'attempted', 'issued', 'confirmed', 'hardware_effect', 'retained', 'retained_effect'] as $key) {
            $passiveLifecycleClear = $passiveLifecycleClear && ($effectiveLifecycle[$key] ?? null) === false;
        }
        $lifecycleEffectValid = $effectiveAction === 'PASSIVE_NORMAL'
            ? ($effectConfirmed && $passiveLifecycleClear)
            : ($effectConfirmed === $activeEffectLifecycle);
        $expectedStatus = in_array($effectiveAction, $knownEffectiveActions, true)
            ? 'DIRECT_MARKETING_' . $effectiveAction . '_' . ($effectConfirmed ? 'EFFECTIVE' : 'PENDING')
            : '';
        $statusValid = $expectedStatus !== '' && $effectiveStatus === $expectedStatus;
        $targetAuthorized = $effectiveStoragePlan['target_projection_authorized'] ?? null;
        $effectivePowerW = $effectiveStoragePlan['effective_power_w'] ?? null;
        $effectiveChargeW = $effectiveStoragePlan['effective_charge_w'] ?? null;
        $curveEffect = $effectConfirmed && in_array($effectiveAction, ['PV_STORE', 'DV_CURVE_CHARGE', 'PASSIVE_NORMAL'], true);
        $expectedTargetAuthorization = $effectConfirmed ? $curveEffect : null;
        $effectValuesValid = $targetAuthorized === $expectedTargetAuthorization
            && ($effectConfirmed
                ? (is_numeric($effectivePowerW) && is_numeric($effectiveChargeW))
                : ($effectivePowerW === null && $effectiveChargeW === null))
            && (!$effectConfirmed || $effectiveAction !== 'CHARGE_BLOCK_WAIT'
                || ((float)$effectivePowerW === 0.0 && (float)$effectiveChargeW === 0.0))
            && (!$curveEffect
                || ((float)$effectivePowerW > 0 && (float)$effectiveChargeW === (float)$effectivePowerW))
            && (!$effectConfirmed || !in_array($effectiveAction, ['ECONOMIC_EXPORT', 'HEADROOM_EXPORT'], true)
                || ((float)$effectivePowerW > 0 && (float)$effectiveChargeW === 0.0));
        $effectiveRevision = (string)($effectiveStoragePlan['revision'] ?? '');
        $effectiveRevisionMaterial = $effectiveStoragePlan;
        unset($effectiveRevisionMaterial['revision']);
        $effectiveRevisionJson = liveTrajectoryCanonicalJson($effectiveRevisionMaterial);
        $calculatedEffectiveRevision = is_string($effectiveRevisionJson)
            ? 'sha256:' . hash('sha256', $effectiveRevisionJson)
            : '';
        $effectiveRevisionValid = preg_match('/^sha256:[0-9a-f]{64}$/', $effectiveRevision) === 1
            && $calculatedEffectiveRevision !== ''
            && hash_equals($effectiveRevision, $calculatedEffectiveRevision);
        $effectiveSlot = null;
        foreach ($storPlan['slots'] as $planSlot) {
            if (is_array($planSlot)
                && ($planSlot['slot_id'] ?? null) === ($runtime['slot_id'] ?? null)) {
                $effectiveSlot = $planSlot;
                break;
            }
        }
        $slotStartMs = is_array($effectiveSlot) ? (int)($effectiveSlot['start_ts_ms'] ?? 0) : 0;
        $slotEndMs = is_array($effectiveSlot) ? (int)($effectiveSlot['end_ts_ms'] ?? 0) : 0;
        $windowStartMs = (int)($effectiveBinding['window_start_ts_ms'] ?? 0);
        $windowEndMs = (int)($effectiveBinding['window_end_ts_ms'] ?? 0);
        $timeBindingValid = $slotStartMs > 0 && $slotEndMs > $slotStartMs
            && (int)($effectiveBinding['slot_start_ts_ms'] ?? 0) === $slotStartMs
            && (int)($effectiveBinding['slot_end_ts_ms'] ?? 0) === $slotEndMs
            && $slotStartMs <= $runtimeTsMs && $runtimeTsMs < $slotEndMs
            && $slotStartMs <= $nowMs && $nowMs < $slotEndMs
            && $windowStartMs > 0 && $windowEndMs > $windowStartMs
            && $windowStartMs <= $runtimeTsMs && $runtimeTsMs < $windowEndMs
            && $windowStartMs <= $nowMs && $nowMs < $windowEndMs;
        $effectiveStoragePlanBound = $canonicalPlan
            && ($runtime['schema_version'] ?? '') === 'storage_dispatch_runtime_v1'
            && ($runtime['owner'] ?? '') === 'storage_manager'
            && ($runtime['plan_valid'] ?? null) === true
            && $runtimeTsMs > 0 && $runtimeAgeMs >= -5000 && $runtimeAgeMs <= 60000
            && ($effectiveStoragePlan['schema_version'] ?? '') === 'storage_effective_plan_v1'
            && ($effectiveStoragePlan['consistent'] ?? null) === true
            && $effectiveRevisionValid && $statusValid && $selectionValid
            && $lifecycleEffectValid && $effectValuesValid
            && $lifecycleBound && $timeBindingValid
            && ($effectiveBinding['plan_id'] ?? null) === ($storPlan['plan_id'] ?? null)
            && ($effectiveBinding['plan_id'] ?? null) === ($runtime['plan_id'] ?? null)
            && ($effectiveBinding['slot_id'] ?? null) === ($runtime['slot_id'] ?? null)
            && ($effectiveBinding['owner'] ?? '') === 'storage_manager'
            && ($effectiveBinding['runtime_generated_at_ts_ms'] ?? null) === ($runtime['runtime_generated_at_ts_ms'] ?? null)
            && preg_match('/^sha256:[0-9a-f]{64}$/', (string)($effectiveBinding['action_id'] ?? '')) === 1
            && trim((string)($effectiveBinding['window_id'] ?? '')) !== ''
            && trim((string)($effectiveBinding['segment_id'] ?? '')) !== ''
            && ($effectiveBinding['action'] ?? '') === ($effectiveStoragePlan['effective_action'] ?? '')
            && ($runtimeAction === $effectiveAction || ($effectiveAction === 'PASSIVE_NORMAL' && $runtimeAction === ''));
        $activeDirectMarketingPlan = is_array($directMarketingForCurve)
            && ($directMarketingForCurve['active'] ?? null) === true
            && ($directMarketingForCurve['shadow'] ?? false) !== true;
        $directMarketingTrajectoryForDisplay = liveDirectMarketingTrajectoryForDisplay(
            $storPlan,
            $directMarketingConfigured,
            $canonicalPlan
        );
        $effectiveProjectionHidden = liveDirectMarketingShouldHideClassicalCurves(
            $activeDirectMarketingPlan,
            $directMarketingTrajectoryForDisplay,
            $effectiveStoragePlanBound,
            $effectiveStoragePlan['target_projection_authorized'] ?? null
        );
        if ($effectiveStoragePlanBound) {
            $data['effective_storage_plan'] = $effectiveStoragePlan;
        }
        $data['direct_marketing_trajectory'] = $directMarketingTrajectoryForDisplay;
        $data['direct_marketing_selected_action_fallback'] = liveDirectMarketingSelectedActionFallbackForDisplay(
            $storPlan,
            $directMarketingConfigured,
            $canonicalPlan,
            $storPlanRawJson,
            $storageActionProjectionArtifact
        );
        // Reine, plan-ID-gebundene Forecastprojektion für den Wärme-Owner.
        // PHP trifft hier weder eine Preis- noch eine Wärmeentscheidung. P50-
        // Werte bleiben Diagnose; eine aktive Freigabe braucht später einen
        // separat validierten konservativen Intent- und Budgetvertrag.
        $heatPriceBoostSlots = [];
        if ($canonicalPlan) {
            foreach ($storPlan['slots'] as $slot) {
                if (!is_array($slot)) continue;
                $slotForecast = is_array($slot['forecast_w'] ?? null) ? $slot['forecast_w'] : [];
                $slotEvidence = is_array($slotForecast['evidence'] ?? null) ? $slotForecast['evidence'] : [];
                $slotProjection = is_array($slot['projection'] ?? null) ? $slot['projection'] : [];
                $heatPriceBoostSlots[] = [
                    'start_ts_ms' => $slot['start_ts_ms'] ?? null,
                    'end_ts_ms' => $slot['end_ts_ms'] ?? null,
                    'slot_id' => $slot['slot_id'] ?? null,
                    'pv_p10_w' => $slotForecast['pv']['p10'] ?? null,
                    'pv_p50_w' => $slotForecast['pv']['p50'] ?? null,
                    'pv_p90_w' => $slotForecast['pv']['p90'] ?? null,
                    'load_p10_w' => $slotForecast['load']['p10'] ?? null,
                    'load_p50_w' => $slotForecast['load']['p50'] ?? null,
                    'load_p90_w' => $slotForecast['load']['p90'] ?? null,
                    'house_p50_w' => $slotForecast['house']['p50'] ?? null,
                    'heat_p50_w' => $slotForecast['heat']['p50'] ?? null,
                    'wallbox_p50_w' => $slotForecast['wallbox']['p50'] ?? null,
                    'external_ac_pv_p50_w' => $slotForecast['external_ac_pv']['p50'] ?? null,
                    'home_source' => $slotProjection['home_source'] ?? null,
                    'home_quality' => $slotProjection['home_quality'] ?? null,
                    'climate_source' => $slotProjection['climate_source'] ?? null,
                    'climate_quality' => $slotProjection['climate_quality'] ?? null,
                    'pv_forecast_fresh' => ($slotEvidence['pv_fresh'] ?? null) === true,
                    'forecast_fresh' => ($slotEvidence['pv_fresh'] ?? null) === true
                        && ($slotEvidence['load_valid'] ?? null) === true,
                    'load_valid' => ($slotEvidence['load_valid'] ?? null) === true,
                ];
            }
        }
        $data['heat_price_boost_forecast'] = [
            'schema_version' => 'heat_price_boost_forecast_v1',
            'status' => $canonicalPlan ? 'canonical_projection' : 'storage_plan_not_canonical',
            'shadow_only' => true,
            'commands_allowed' => false,
            'plan_id' => $canonicalPlan ? ($storPlan['plan_id'] ?? null) : null,
            'input_revisions' => $canonicalPlan ? ($storPlan['input_revisions'] ?? null) : null,
            'candidate' => $canonicalPlan ? ($storPlan['heat_intent_candidate'] ?? null) : null,
            'slots' => $heatPriceBoostSlots,
        ];
        $dispatchSlots = $canonicalPlan ? $storPlan['slots'] : ($storPlan['timeline'] ?? []);
        foreach ($dispatchSlots as $slot) {
            if (!is_array($slot)) continue;
            $tsMs = $canonicalPlan
                ? (int)($slot['start_ts_ms'] ?? 0)
                : (int)($slot['ts'] ?? 0);
            $ts = (int)floor($tsMs / 1000);
            if ($ts < $today0 || $ts >= $today1) continue;
            $projection = $canonicalPlan && is_array($slot['projection'] ?? null)
                ? $slot['projection']
                : [
                    'soc_pct' => $slot['soc'] ?? null,
                    'target_soc_pct' => null,
                    'pv_w' => $slot['pv_w'] ?? 0,
                    'direct_marketing_export_w' => 0,
                    'direct_marketing_planned_w' => 0,
                    'direct_marketing_charge_w' => 0,
                    'direct_marketing_soc_pct' => null,
                    'direct_marketing_action' => null,
                ];
            $soc = isset($projection['soc_pct']) && is_numeric($projection['soc_pct'])
                ? (float)$projection['soc_pct']
                : null;
            if ($effectiveProjectionHidden) {
                $soc = null;
            }
            $targetSoc = isset($projection['target_soc_pct']) && is_numeric($projection['target_soc_pct'])
                ? (float)$projection['target_soc_pct']
                : null;
            $reserveFloor = $canonicalPlan && isset($slot['soc_pct']['reserve_floor']) && is_numeric($slot['soc_pct']['reserve_floor'])
                ? (float)$slot['soc_pct']['reserve_floor']
                : null;
            $ceiling = $canonicalPlan && isset($slot['soc_pct']['ceiling']) && is_numeric($slot['soc_pct']['ceiling'])
                ? (float)$slot['soc_pct']['ceiling']
                : null;
            if (!$effectiveProjectionHidden && $targetSoc !== null) {
                $targetCurve[] = ['ts' => $tsMs, 'soc' => round($targetSoc, 1)];
            }
            if ($reserveFloor !== null) $socMinCurve[] = ['ts' => $tsMs, 'soc' => round($reserveFloor, 1)];
            if ($ceiling !== null) $socCeilingCurve[] = ['ts' => $tsMs, 'soc' => round($ceiling, 1)];
            $projectionPlanAction = strtoupper((string)($projection['direct_marketing_plan_action'] ?? ''));
            $projectionPlanSelected = $directMarketingConfigured
                && !empty($projection['direct_marketing_selected'])
                && !empty($projection['direct_marketing_plan_executable'])
                && !empty($projection['direct_marketing_plan_commands_allowed']);
            $projectionPvStoreSelected = $projectionPlanSelected
                && $projectionPlanAction === 'PV_STORE';
            $projectionEconomicExportSelected = $projectionPlanSelected
                && $projectionPlanAction === 'ECONOMIC_EXPORT';
            $simCurve[] = [
                'ts' => $tsMs,
                'plan_id' => $canonicalPlan ? ($storPlan['plan_id'] ?? null) : null,
                'slot_id' => $canonicalPlan ? ($slot['slot_id'] ?? null) : null,
                'soc' => $soc !== null ? round($soc, 1) : null,
                'pv_w' => (int)round((float)($projection['pv_w'] ?? 0)),
                'direct_marketing_slot_projection_w' => $canonicalPlan
                    && isset($projection['battery_w'])
                    && is_numeric($projection['battery_w'])
                    ? (int)round((float)$projection['battery_w'])
                    : null,
                'direct_marketing_slot_projection_wh' => $canonicalPlan
                    && isset($slot['planned_wh']['charge'])
                    && is_numeric($slot['planned_wh']['charge'])
                    ? round((float)$slot['planned_wh']['charge'], 3)
                    : null,
                'direct_marketing_export_w' => $projectionEconomicExportSelected
                    ? (int)round(max(0.0, (float)($projection['direct_marketing_export_w'] ?? 0)))
                    : 0,
                'direct_marketing_planned_w' => $projectionPlanSelected
                    ? (int)round(max(0.0, (float)($projection['direct_marketing_planned_w'] ?? 0)))
                    : 0,
                'direct_marketing_charge_w' => $projectionPvStoreSelected
                    ? (int)round(max(
                        0.0,
                        (float)($projection['direct_marketing_charge_w'] ?? 0),
                        (float)($projection['direct_marketing_planned_w'] ?? 0)
                    ))
                    : 0,
                'direct_marketing_soc' => $directMarketingConfigured
                    && isset($projection['direct_marketing_soc_pct'])
                    && is_numeric($projection['direct_marketing_soc_pct'])
                    ? round((float)$projection['direct_marketing_soc_pct'], 1)
                    : null,
                'direct_marketing_action' => $projectionPlanSelected
                    ? ($projection['direct_marketing_plan_action'] ?? null)
                    : null,
                'direct_marketing_window_id' => $directMarketingConfigured
                    ? ($projection['direct_marketing_window_id'] ?? null)
                    : null,
                'direct_marketing_export_segment_id' => $directMarketingConfigured
                    ? ($projection['direct_marketing_export_segment_id'] ?? null)
                    : null,
            ];
        }
        if (!$canonicalPlan) {
            foreach (($storPlan['target_timeline'] ?? []) as $slot) {
                $ts = (int)(($slot['ts'] ?? 0) / 1000);
                if ($ts >= $today0 && $ts < $today1) {
                    $targetCurve[] = ['ts' => $ts * 1000, 'soc' => round((float)$slot['soc'], 1)];
                }
            }
            foreach (($storPlan['soc_min_curve'] ?? []) as $slot) {
                $ts = (int)(($slot['ts'] ?? 0) / 1000);
                if ($ts >= $today0 && $ts < $today1) {
                    $socMinCurve[] = ['ts' => $ts * 1000, 'soc' => round((float)$slot['soc'], 1)];
                }
            }
            foreach (($storPlan['soc_ceiling_curve'] ?? []) as $slot) {
                $ts = (int)(($slot['ts'] ?? 0) / 1000);
                if ($ts >= $today0 && $ts < $today1) {
                    $socCeilingCurve[] = ['ts' => $ts * 1000, 'soc' => round((float)$slot['soc'], 1)];
                }
            }
        }
        $simMaxSoc = null;
        foreach ($simCurve as $slot) {
            $simMaxSoc = max($simMaxSoc ?? 0, (float)($slot['soc'] ?? 0));
        }
        foreach (($storPlan['curve_anchors'] ?? []) as $slot) {
            $ts = (int)($slot['ts'] / 1000);
            if ($ts >= $today0 && $ts < $today1) {
                $curveAnchors[] = [
                    'ts' => $ts * 1000,
                    'soc' => round((float)$slot['soc'], 1),
                    't' => $slot['t'] ?? date('H:i', $ts),
                    'kind' => $slot['kind'] ?? 'hourly',
                    'frozen' => !empty($slot['frozen']),
                ];
            }
        }
        if ($effectiveProjectionHidden) {
            // Bei aktiver DV bleibt die physikalische Standard-Ladekurve als Referenz
            // für das Ladekurven-Modal und die Dashboard-Kachel erhalten.
        }
        if (empty($data['ladekurve']) && (!empty($targetCurve) || !empty($simCurve))) {
            $tzName = $confData['config']['timezone'] ?? 'Europe/Berlin';
            try {
                $storageTz = new DateTimeZone((string)$tzName);
            } catch (Exception $e) {
                $storageTz = new DateTimeZone('Europe/Berlin');
            }
            $fmtCurveTime = function ($tsMs) use ($storageTz) {
                $sec = (int)round(((float)$tsMs) / 1000);
                $dt = new DateTime('@' . $sec);
                $dt->setTimezone($storageTz);
                return $dt->format('H:i');
            };
            $socNear = function ($tsMs, $fallback) use ($targetCurve) {
                if (empty($targetCurve)) {
                    return $fallback;
                }
                $best = null;
                $bestDelta = PHP_INT_MAX;
                foreach ($targetCurve as $point) {
                    $delta = abs((float)($point['ts'] ?? 0) - (float)$tsMs);
                    if ($delta < $bestDelta) {
                        $bestDelta = $delta;
                        $best = $point;
                    }
                }
                return $best !== null ? (float)($best['soc'] ?? $fallback) : $fallback;
            };

            $firstPoint = !empty($targetCurve) ? $targetCurve[0] : $simCurve[0];
            $lastPoint = !empty($targetCurve) ? $targetCurve[count($targetCurve) - 1] : $simCurve[count($simCurve) - 1];
            $firstTs = (float)($firstPoint['ts'] ?? 0);
            $firstSoc = (float)($firstPoint['soc'] ?? 0);
            $lastTs = (float)($lastPoint['ts'] ?? $firstTs);
            $lastSoc = (float)($lastPoint['soc'] ?? $firstSoc);

            $peak = null;
            if (!empty($simCurve)) {
                $peakSlot = null;
                foreach ($simCurve as $slot) {
                    if ($peakSlot === null || (float)($slot['pv_w'] ?? 0) > (float)($peakSlot['pv_w'] ?? 0)) {
                        $peakSlot = $slot;
                    }
                }
                $peakPvW = (float)($peakSlot['pv_w'] ?? 0);
                if ($peakPvW > 500) {
                    $peakTs = (float)($peakSlot['ts'] ?? $firstTs);
                    $peak = [
                        't' => $fmtCurveTime($peakTs),
                        'soc' => round($socNear($peakTs, (float)($peakSlot['soc'] ?? $firstSoc)), 1),
                        'pv_kw' => round($peakPvW / 1000, 1),
                        'past' => $peakTs < (time() * 1000),
                        'source' => 'storage_plan',
                    ];
                }
            }
            if ($peak === null) {
                $nearest = $firstPoint;
                $nearestDelta = PHP_INT_MAX;
                $nowMs = time() * 1000;
                foreach ($targetCurve ?: [$firstPoint] as $point) {
                    $delta = abs((float)($point['ts'] ?? 0) - $nowMs);
                    if ($delta < $nearestDelta) {
                        $nearestDelta = $delta;
                        $nearest = $point;
                    }
                }
                $peakTs = (float)($nearest['ts'] ?? $firstTs);
                $peak = [
                    't' => $fmtCurveTime($peakTs),
                    'soc' => round((float)($nearest['soc'] ?? $firstSoc), 1),
                    'past' => $peakTs < $nowMs,
                    'source' => 'target_timeline',
                ];
            }

            $freilaufTs = (float)($storPlan['ladeende_ts'] ?? ($targetCurveMeta['curve_end_ts'] ?? $lastTs));
            if (!($freilaufTs >= ($today0 * 1000) && $freilaufTs < ($today1 * 1000))) {
                $freilaufTs = $lastTs;
            }
            $freilaufSoc = (float)($storPlan['ladeende_soc'] ?? ($storPlan['effective_target_soc'] ?? ($storPlan['target_soc'] ?? $lastSoc)));
            $todayLocal0 = strtotime(date('Y-m-d') . ' 00:00:00') ?: $today0;
            $data['ladekurve'] = [
                'day_label' => $storageDisplayDayLabel,
                'day_offset' => (int)round(($today0 - $todayLocal0) / 86400),
                'day_start_ts' => $today0 * 1000,
                'date' => date('Y-m-d', $today0),
                'has_target_curve' => !empty($targetCurve),
                'ladestart' => [
                    't' => $fmtCurveTime($firstTs),
                    'soc' => round($firstSoc, 1),
                    'past' => $firstTs < (time() * 1000),
                    'forecast' => $today0 > $todayLocal0,
                ],
                'peak' => $peak,
                'freilauf' => [
                    't' => $fmtCurveTime($freilaufTs),
                    'soc' => round($freilaufSoc, 1),
                    'past' => $freilaufTs < (time() * 1000),
                ],
            ];
        }
        $data['storage_target_curve'] = $targetCurve;
        if (!empty($socMinCurve)) {
            $data['storage_soc_min_curve'] = $socMinCurve;
        }
        if (!empty($socCeilingCurve)) {
            $data['storage_soc_ceiling_curve'] = $socCeilingCurve;
        }
        $data['storage_sim_curve'] = $simCurve;
        $data['storage_curve_anchors'] = $curveAnchors;
        // Metadaten: Ziel-SoC, erreichbarer Max-SoC, Q-Ratio
        $cheapGridCharge = (isset($storPlan['cheap_grid_charge']) && is_array($storPlan['cheap_grid_charge']))
            ? $storPlan['cheap_grid_charge']
            : null;
        if ($cheapGridCharge !== null) {
            $data['cheap_grid_charge'] = $cheapGridCharge;
        }
        $directMarketing = $directMarketingForCurve;
        if ($directMarketing !== null) {
            $data['direct_marketing'] = $directMarketing;
        }
        $marketPlan = (isset($storPlan['market_plan']) && is_array($storPlan['market_plan']))
            ? $storPlan['market_plan']
            : null;
        if ($marketPlan !== null) {
            $data['market_plan'] = $marketPlan;
        }
        $marketPlanSummary = (is_array($marketPlan) && isset($marketPlan['summary']) && is_array($marketPlan['summary']))
            ? $marketPlan['summary']
            : null;
        if ($marketPlanSummary !== null) {
            $data['market_plan_summary'] = $marketPlanSummary;
        }
        $directMarketingMonitor = (isset($data['direct_marketing_monitor']) && is_array($data['direct_marketing_monitor']))
            ? $data['direct_marketing_monitor']
            : null;
        $directMarketingDailyReport = (isset($data['direct_marketing_daily_report']) && is_array($data['direct_marketing_daily_report']))
            ? $data['direct_marketing_daily_report']
            : null;
        $marketValueSolar = (isset($data['market_value_solar']) && is_array($data['market_value_solar']))
            ? $data['market_value_solar']
            : null;
        if ($directMarketingMonitor !== null) {
            $pvStoreDcOnlyConfigured = !empty($directMarketingMonitor['pv_store_dc_only'])
                || !empty($directMarketingMonitor['direct_marketing_pv_store_dc_only']);
            $policyTargetState = strtoupper(trim((string)($directMarketingMonitor['policy_target_state'] ?? '')));
            $monitorDecisionState = strtolower(trim((string)($directMarketingMonitor['decision_state'] ?? $directMarketingMonitor['state'] ?? '')));
            $monitorAction = strtolower(trim((string)($directMarketingMonitor['current_action'] ?? '')));
            $pvStoreOwnerActive = !empty($directMarketingMonitor['active']) && (
                $policyTargetState === 'FORCE_CHARGE_PV'
                || str_contains($monitorDecisionState, 'pv_store')
                || str_contains($monitorAction, 'store_pv')
            );
            $pvStoreDcOnlyActive = $pvStoreDcOnlyConfigured && $pvStoreOwnerActive;
            $externalGuardW = isset($directMarketingMonitor['pv_store_external_ac_guard_w'])
                ? (int)$directMarketingMonitor['pv_store_external_ac_guard_w']
                : (isset($directMarketingMonitor['direct_marketing_pv_store_external_ac_guard_w'])
                    ? (int)$directMarketingMonitor['direct_marketing_pv_store_external_ac_guard_w']
                    : 0);
            $externalPvW = isset($directMarketingMonitor['pv_external_ac_w'])
                ? (int)$directMarketingMonitor['pv_external_ac_w']
                : (int)($data['pv_external_w'] ?? 0);
            $data['pv_dc_only_configured'] = $pvStoreDcOnlyConfigured;
            $data['pv_dc_only_active'] = $pvStoreDcOnlyActive;
            $data['pv_external_charge_guard_w'] = max(0, $externalGuardW);
            $data['pv_external_charge_locked'] = $pvStoreDcOnlyActive && $externalPvW > max(0, $externalGuardW);
            $data['pv_external_charge_lock_reason'] = $data['pv_external_charge_locked']
                ? 'Nur E3DC-DC-PV laden: externer AC-Zusatzwechselrichter wird nicht als Akku-Ladequelle genutzt.'
                : '';
        }
        $effectiveValue = static function ($key, $fallback) use (
            $effectiveStoragePlanBound,
            $effectiveStoragePlan,
            $activeDirectMarketingPlan
        ) {
            if ($effectiveStoragePlanBound && array_key_exists($key, $effectiveStoragePlan)) {
                return $effectiveStoragePlan[$key];
            }
            return $activeDirectMarketingPlan ? null : $fallback;
        };
        $displayTargetSoc = $effectiveValue('target_soc', $storPlan['target_soc'] ?? null);
        $displayMaxSoc = $effectiveValue('sim_max_soc_pct', $storPlan['max_soc_pct'] ?? $simMaxSoc);
        $displayCanReachTarget = $effectiveValue(
            'can_reach_target',
            array_key_exists('can_reach_target', $storPlan) ? (bool)$storPlan['can_reach_target'] : true
        );
        if (!$activeDirectMarketingPlan && !array_key_exists('can_reach_target', $storPlan) && $displayMaxSoc !== null && $displayTargetSoc !== null) {
            $displayCanReachTarget = ((float)$displayMaxSoc >= ((float)$displayTargetSoc - 0.2));
        }
        $targetReachState = $effectiveValue(
            'target_reach_state',
            $storPlan['target_reach_state'] ?? ($targetCurveMeta['target_reach_state'] ?? ($displayCanReachTarget ? 'reachable' : 'unreachable_auto'))
        );
        $targetReachMode = $activeDirectMarketingPlan
            ? 'direct_marketing'
            : ($storPlan['target_reach_mode']
                ?? ($targetCurveMeta['target_reach_mode'] ?? ($displayCanReachTarget ? 'curve_servo' : 'e3dc_auto')));
        $targetReachReason = $effectiveValue(
            'target_reach_reason',
            $storPlan['target_reach_reason'] ?? ($targetCurveMeta['target_reach_reason'] ?? '')
        );
        if ($targetReachReason === null || $targetReachReason === '') {
            $targetReachReason = $activeDirectMarketingPlan
                ? 'DV-Auswahl oder Hardwarewirkung ist noch nicht vollständig gebunden; Ziel und Leistung bleiben unbekannt.'
                : ($displayCanReachTarget
                ? 'Tagesziel erreichbar: Zielkurve aktiv. Die Prognose wird bei jedem Planlauf neu geprüft.'
                : 'Tagesziel aktuell nicht erreichbar: E3DC AUTO. Der E3DC nutzt realen PV-Überschuss autonom; Entladung bleibt geschützt. Die Prognose wird bei jedem Planlauf neu geprüft.');
        }
        if ($activeDirectMarketingPlan) {
            $data['target_soc'] = $displayTargetSoc;
            $data['storage_ifc_w'] = $effectiveStoragePlanBound
                ? ($effectiveStoragePlan['effective_charge_w'] ?? null)
                : null;
            $data['storage_charge_request_w'] = $data['storage_ifc_w'];
            $data['storage_curve_soc_now'] = $effectiveValue('current_curve_soc', null);
            $data['storage_curve_soc_target'] = $effectiveValue('next_curve_soc', null);
            $data['storage_curve_target_ts'] = $effectiveValue('next_curve_ts', null);
        }
        $targetReachRecheckActive = $storPlan['target_reach_recheck_active']
            ?? ($targetCurveMeta['target_reach_recheck_active'] ?? true);
        $weatherReserveActive = !empty($storPlan['weather_reserve_active']) || !empty($targetCurveMeta['weather_reserve_active']);
        $weatherReserveNeedWh = $storPlan['weather_reserve_need_wh'] ?? ($targetCurveMeta['weather_reserve_need_wh'] ?? null);
        $weatherReserveReason = $targetCurveMeta['weather_reserve_reason']
            ?? ($weatherReserveActive ? 'Schlechte Mehrtageprognose: Pre-Dump pausiert, Energie bleibt im Speicher.' : null);
        $data['storage_plan_meta'] = [
            'schema_version'  => $storPlan['schema_version'] ?? null,
            'plan_id'         => $storPlan['plan_id'] ?? null,
            'generated_at'    => $storPlan['generated_at'] ?? null,
            'valid_from'      => $storPlan['valid_from'] ?? null,
            'valid_until'     => $storPlan['valid_until'] ?? null,
            'horizon_end'     => $storPlan['horizon_end'] ?? null,
            'input_revisions' => $storPlan['input_revisions'] ?? null,
            'planner'         => $storPlan['planner'] ?? null,
            'target_soc'      => $displayTargetSoc,
            'planning_target_soc'=> $effectiveValue('planning_target_soc', $storPlan['planning_target_soc'] ?? ($targetCurveMeta['planning_target_soc'] ?? null)),
            'effective_target_soc'=> $effectiveValue('effective_target_soc', $storPlan['effective_target_soc'] ?? null),
            'effective_storage_plan'=> $effectiveStoragePlanBound ? $effectiveStoragePlan : null,
            'effective_projection_status'=> $effectiveStoragePlanBound ? $effectiveStatus : 'EVIDENCE_LIMIT',
            'target_projection_authorized'=> $effectiveStoragePlanBound
                ? ($effectiveStoragePlan['target_projection_authorized'] ?? null)
                : ($activeDirectMarketingPlan ? null : true),
            'clear_classical_curves'=> $effectiveProjectionHidden,
            'morning_target'  => $storPlan['morning_target']  ?? null,
            'morning_hour'    => $storPlan['morning_hour']    ?? null,
            'predump_enabled' => $storPlan['predump_enabled'] ?? null,
            'predump_min_soc' => $storPlan['predump_min_soc'] ?? null,
            'predump_dump_wh' => $storPlan['predump_dump_wh'] ?? ($targetCurveMeta['predump_dump_wh'] ?? 0),
            'predump_preventable_clipping_wh' => $storPlan['predump_preventable_clipping_wh'] ?? ($targetCurveMeta['predump_preventable_clipping_wh'] ?? 0),
            'predump_unavoidable_clipping_wh' => $storPlan['predump_unavoidable_clipping_wh'] ?? ($targetCurveMeta['predump_unavoidable_clipping_wh'] ?? 0),
            'predump_reason' => $storPlan['predump_reason'] ?? ($targetCurveMeta['predump_reason'] ?? ''),
            'predump_start_ts' => $storPlan['predump_start_ts'] ?? ($targetCurveMeta['predump_start_ts'] ?? 0),
            'predump_end_ts' => $storPlan['predump_end_ts'] ?? ($targetCurveMeta['predump_end_ts'] ?? 0),
            'hard_predump_enabled' => $storPlan['hard_predump_enabled'] ?? ($targetCurveMeta['hard_predump_enabled'] ?? false),
            'hard_predump_target_soc' => $storPlan['hard_predump_target_soc'] ?? ($targetCurveMeta['hard_predump_target_soc'] ?? null),
            'hard_predump_grid_enabled' => $storPlan['hard_predump_grid_enabled'] ?? ($targetCurveMeta['hard_predump_grid_enabled'] ?? false),
            'weather_reserve_active' => $weatherReserveActive,
            'weather_reserve_need_wh' => $weatherReserveNeedWh,
            'weather_reserve_reason' => $weatherReserveReason,
            'eco_dump_date'   => $storPlan['eco_dump_date']   ?? '',
            'config_morning_soc' => $confData['config']['storage_morning_soc'] ?? null,
            'config_predump_enabled' => $confData['config']['predump_enable'] ?? null,
            'config_headroom_discharge_enabled' => $confData['config']['storage_headroom_discharge_enable'] ?? '1',
            'config_headroom_discharge_daily_limit_pct' => $confData['config']['storage_headroom_discharge_daily_limit_pct'] ?? '10',
            'config_headroom_discharge_cooldown_min' => $confData['config']['storage_headroom_discharge_cooldown_min'] ?? '10',
            'config_headroom_discharge_target_plateau_margin_pct' => $confData['config']['storage_headroom_discharge_target_plateau_margin_pct'] ?? '0.3',
            'config_predump_min_soc' => $confData['config']['storage_predump_min_soc'] ?? ($storPlan['predump_min_soc'] ?? null),
            'config_hard_predump_enabled' => $confData['config']['hard_predump_enable'] ?? null,
            'config_hard_predump_target_soc' => $confData['config']['hard_predump_target_soc'] ?? null,
            'config_hard_predump_grid_enabled' => $confData['config']['hard_predump_grid_enable'] ?? '0',
            'ladestart_soc'   => $effectiveProjectionHidden ? null : ($storPlan['ladestart_soc'] ?? null),
            'ladestart_ts'    => $effectiveProjectionHidden ? null : ($storPlan['ladestart_ts'] ?? null),
            'start_anchor_ts' => $effectiveProjectionHidden ? null : ($targetCurveMeta['start_anchor_ts'] ?? ($storPlan['ladestart_ts'] ?? null)),
            'start_anchor_t'  => $effectiveProjectionHidden ? null : ($targetCurveMeta['start_anchor_t'] ?? null),
            'start_anchor_soc'=> $effectiveProjectionHidden ? null : ($targetCurveMeta['start_anchor_soc'] ?? ($storPlan['ladestart_soc'] ?? null)),
            'pv_forecast_kwh' => $storPlan['pv_forecast_kwh'] ?? ($targetCurveMeta['pv_forecast_kwh'] ?? null),
            'adaptive_headroom_required_wh' => $storPlan['adaptive_headroom_required_wh'] ?? ($targetCurveMeta['adaptive_headroom_required_wh'] ?? 0),
            'adaptive_headroom_available_wh' => $storPlan['adaptive_headroom_available_wh'] ?? ($targetCurveMeta['adaptive_headroom_available_wh'] ?? 0),
            'adaptive_headroom_need_without_buffer_wh' => $storPlan['adaptive_headroom_need_without_buffer_wh'] ?? ($targetCurveMeta['adaptive_headroom_need_without_buffer_wh'] ?? 0),
            'adaptive_headroom_buffer_wh' => $storPlan['adaptive_headroom_buffer_wh'] ?? ($targetCurveMeta['adaptive_headroom_buffer_wh'] ?? 0),
            'adaptive_soc_floor' => $storPlan['adaptive_soc_floor'] ?? ($targetCurveMeta['adaptive_soc_floor'] ?? null),
            'adaptive_soc_ceiling' => $storPlan['adaptive_soc_ceiling'] ?? ($targetCurveMeta['adaptive_soc_ceiling'] ?? null),
            'adaptive_soc_ceiling_raw' => $storPlan['adaptive_soc_ceiling_raw'] ?? ($targetCurveMeta['adaptive_soc_ceiling_raw'] ?? null),
            'adaptive_headroom_floor_conflict' => $storPlan['adaptive_headroom_floor_conflict'] ?? ($targetCurveMeta['adaptive_headroom_floor_conflict'] ?? false),
            'adaptive_headroom_floor_conflict_points' => $storPlan['adaptive_headroom_floor_conflict_points'] ?? ($targetCurveMeta['adaptive_headroom_floor_conflict_points'] ?? 0),
            'adaptive_headroom_floor_conflict_max_delta_pct' => $storPlan['adaptive_headroom_floor_conflict_max_delta_pct'] ?? ($targetCurveMeta['adaptive_headroom_floor_conflict_max_delta_pct'] ?? 0),
            'published_curve_floor_policy' => $storPlan['published_curve_floor_policy'] ?? ($targetCurveMeta['published_curve_floor_policy'] ?? ''),
            'published_curve_floor_active' => $storPlan['published_curve_floor_active'] ?? ($targetCurveMeta['published_curve_floor_active'] ?? false),
            'published_curve_floor_source' => $storPlan['published_curve_floor_source'] ?? ($targetCurveMeta['published_curve_floor_source'] ?? ''),
            'published_curve_floor_reason' => $storPlan['published_curve_floor_reason'] ?? ($targetCurveMeta['published_curve_floor_reason'] ?? ''),
            'published_curve_floor_reset_allowed' => $storPlan['published_curve_floor_reset_allowed'] ?? ($targetCurveMeta['published_curve_floor_reset_allowed'] ?? false),
            'published_curve_floor_reset_reason' => $storPlan['published_curve_floor_reset_reason'] ?? ($targetCurveMeta['published_curve_floor_reset_reason'] ?? ''),
            'published_curve_anchor_clamps' => $storPlan['published_curve_anchor_clamps'] ?? ($targetCurveMeta['published_curve_anchor_clamps'] ?? 0),
            'published_curve_timeline_clamps' => $storPlan['published_curve_timeline_clamps'] ?? ($targetCurveMeta['published_curve_timeline_clamps'] ?? 0),
            'published_curve_max_lift_pct' => $storPlan['published_curve_max_lift_pct'] ?? ($targetCurveMeta['published_curve_max_lift_pct'] ?? 0),
            'adaptive_storage_class' => $storPlan['adaptive_storage_class'] ?? ($targetCurveMeta['adaptive_storage_class'] ?? null),
            'adaptive_storage_kwh' => $storPlan['adaptive_storage_kwh'] ?? ($targetCurveMeta['adaptive_storage_kwh'] ?? null),
            'adaptive_large_storage_threshold_kwh' => $storPlan['adaptive_large_storage_threshold_kwh'] ?? ($targetCurveMeta['adaptive_large_storage_threshold_kwh'] ?? null),
            'adaptive_comfort_soc' => $storPlan['adaptive_comfort_soc'] ?? ($targetCurveMeta['adaptive_comfort_soc'] ?? null),
            'adaptive_comfort_floor_soc' => $storPlan['adaptive_comfort_floor_soc'] ?? ($targetCurveMeta['adaptive_comfort_floor_soc'] ?? null),
            'adaptive_comfort_active' => $storPlan['adaptive_comfort_active'] ?? ($targetCurveMeta['adaptive_comfort_active'] ?? false),
            'adaptive_comfort_limited_by_headroom' => $storPlan['adaptive_comfort_limited_by_headroom'] ?? ($targetCurveMeta['adaptive_comfort_limited_by_headroom'] ?? false),
            'headroom_reserve_active' => $storPlan['headroom_reserve_active'] ?? ($targetCurveMeta['headroom_reserve_active'] ?? false),
            'headroom_reserve_pressure_wh' => $storPlan['headroom_reserve_pressure_wh'] ?? ($targetCurveMeta['headroom_reserve_pressure_wh'] ?? 0),
            'headroom_reserve_slots' => $storPlan['headroom_reserve_slots'] ?? ($targetCurveMeta['headroom_reserve_slots'] ?? 0),
            'headroom_reserve_floor_protected' => $storPlan['headroom_reserve_floor_protected'] ?? ($targetCurveMeta['headroom_reserve_floor_protected'] ?? false),
            'headroom_reserve_floor_protected_points' => $storPlan['headroom_reserve_floor_protected_points'] ?? ($targetCurveMeta['headroom_reserve_floor_protected_points'] ?? 0),
            'headroom_reserve_floor_protected_max_delta_pct' => $storPlan['headroom_reserve_floor_protected_max_delta_pct'] ?? ($targetCurveMeta['headroom_reserve_floor_protected_max_delta_pct'] ?? 0),
            'headroom_floor_policy' => $storPlan['headroom_floor_policy'] ?? ($targetCurveMeta['headroom_floor_policy'] ?? ''),
            'headroom_reserve_source' => $storPlan['headroom_reserve_source'] ?? ($targetCurveMeta['headroom_reserve_source'] ?? ''),
            'headroom_reserve_live_pv_w' => $storPlan['headroom_reserve_live_pv_w'] ?? ($targetCurveMeta['headroom_reserve_live_pv_w'] ?? 0),
            'headroom_reserve_forecast_now_w' => $storPlan['headroom_reserve_forecast_now_w'] ?? ($targetCurveMeta['headroom_reserve_forecast_now_w'] ?? 0),
            'headroom_reserve_forecast_ratio' => $storPlan['headroom_reserve_forecast_ratio'] ?? ($targetCurveMeta['headroom_reserve_forecast_ratio'] ?? 0),
            'headroom_reserve_max_pv_w' => $storPlan['headroom_reserve_max_pv_w'] ?? ($targetCurveMeta['headroom_reserve_max_pv_w'] ?? 0),
            'headroom_discharge_today_wh' => $data['storage_headroom_discharge_today_wh'] ?? null,
            'headroom_discharge_daily_limit_wh' => $data['storage_headroom_discharge_daily_limit_wh'] ?? null,
            'headroom_discharge_daily_remaining_wh' => $data['storage_headroom_discharge_daily_remaining_wh'] ?? null,
            'headroom_discharge_daily_limit_pct' => $data['storage_headroom_discharge_daily_limit_pct'] ?? null,
            'headroom_discharge_daily_blocked' => $data['storage_headroom_discharge_daily_blocked'] ?? null,
            'headroom_discharge_cooldown_s' => $data['storage_headroom_discharge_cooldown_s'] ?? null,
            'headroom_discharge_cooldown_remaining_s' => $data['storage_headroom_discharge_cooldown_remaining_s'] ?? null,
            'headroom_discharge_cooldown_active' => $data['storage_headroom_discharge_cooldown_active'] ?? null,
            'headroom_discharge_blocked_reason' => $data['storage_headroom_discharge_blocked_reason'] ?? null,
            'headroom_discharge_target_plateau_reached' => $data['storage_headroom_discharge_target_plateau_reached'] ?? null,
            'headroom_discharge_target_curve_soc' => $data['storage_headroom_discharge_target_curve_soc'] ?? null,
            'headroom_discharge_target_plateau_margin_pct' => $data['storage_headroom_discharge_target_plateau_margin_pct'] ?? null,
            'curtailment_pressure_wh' => $storPlan['curtailment_pressure_wh'] ?? ($targetCurveMeta['curtailment_pressure_wh'] ?? 0),
            'curtailment_unavoidable_wh' => $storPlan['curtailment_unavoidable_wh'] ?? ($targetCurveMeta['curtailment_unavoidable_wh'] ?? 0),
            'curtailment_first_pressure_ts' => $storPlan['curtailment_first_pressure_ts'] ?? ($targetCurveMeta['curtailment_first_pressure_ts'] ?? 0),
            'curtailment_soc_at_first_pressure' => $storPlan['curtailment_soc_at_first_pressure'] ?? ($targetCurveMeta['curtailment_soc_at_first_pressure'] ?? null),
            'latest_charge_start_ts' => $effectiveProjectionHidden ? null : ($storPlan['latest_charge_start_ts'] ?? ($targetCurveMeta['latest_charge_start_ts'] ?? 0)),
            'evening_shortfall_wh' => $storPlan['evening_shortfall_wh'] ?? ($targetCurveMeta['evening_shortfall_wh'] ?? 0),
            'can_reach_target'=> $displayCanReachTarget,
            'target_reach_state'=> $targetReachState,
            'target_reach_mode'=> $targetReachMode,
            'target_reach_reason'=> $targetReachReason,
            'target_reach_recheck_active'=> (bool)$targetReachRecheckActive,
            'target_reach_policy'=> $storPlan['target_reach_policy'] ?? ($targetCurveMeta['target_reach_policy'] ?? null),
            'target_reach_control_owner'=> $storPlan['target_reach_control_owner'] ?? ($targetCurveMeta['target_reach_control_owner'] ?? null),
            'target_reach_status_only'=> (bool)($storPlan['target_reach_status_only'] ?? ($targetCurveMeta['target_reach_status_only'] ?? true)),
            'target_reach_changed'=> (bool)($storPlan['target_reach_changed'] ?? ($targetCurveMeta['target_reach_changed'] ?? false)),
            'target_reach_last_change_ts'=> $storPlan['target_reach_last_change_ts'] ?? ($targetCurveMeta['target_reach_last_change_ts'] ?? null),
            'target_reach_stable_s'=> $storPlan['target_reach_stable_s'] ?? ($targetCurveMeta['target_reach_stable_s'] ?? null),
            'target_reach_can_reach_target'=> $activeDirectMarketingPlan
                ? $displayCanReachTarget
                : ($storPlan['target_reach_can_reach_target'] ?? ($targetCurveMeta['target_reach_can_reach_target'] ?? $displayCanReachTarget)),
            'target_reach_surplus_wh'=> $storPlan['target_reach_surplus_wh'] ?? ($targetCurveMeta['target_reach_surplus_wh'] ?? null),
            'target_reach_required_wh'=> $storPlan['target_reach_required_wh'] ?? ($targetCurveMeta['target_reach_required_wh'] ?? null),
            'target_reach_margin_wh'=> $storPlan['target_reach_margin_wh'] ?? ($targetCurveMeta['target_reach_margin_wh'] ?? null),
            'target_reach_sim_max_soc_pct'=> $storPlan['target_reach_sim_max_soc_pct'] ?? ($targetCurveMeta['target_reach_sim_max_soc_pct'] ?? null),
            'target_reach_max_reachable_soc'=> $effectiveValue('max_reachable_soc', $storPlan['target_reach_max_reachable_soc'] ?? ($targetCurveMeta['target_reach_max_reachable_soc'] ?? null)),
            'max_soc_pct'     => $displayMaxSoc,
            'sim_max_soc_pct' => $displayMaxSoc,
            'max_reachable_soc'=> $effectiveValue('max_reachable_soc', $storPlan['max_reachable_soc'] ?? ($targetCurveMeta['max_reachable_soc'] ?? null)),
            'q_ratio'         => $effectiveProjectionHidden ? null : ($storPlan['q_ratio'] ?? null),
            'bat_cap_kwh'     => $storPlan['bat_cap_kwh']     ?? null,
            'target_curve_meta'=> $effectiveProjectionHidden ? [] : $targetCurveMeta,
            'display_day_label'=> $storageDisplayDayLabel,
            'display_day_start'=> $today0 * 1000,
            'display_day_end'  => $today1 * 1000,
            'has_target_curve' => !empty($targetCurve),
            'cheap_grid_charge'=> $cheapGridCharge,
            'market_plan'      => $marketPlan,
            'market_plan_summary'=> $marketPlanSummary,
            'direct_marketing'=> $directMarketing,
            'direct_marketing_monitor'=> $directMarketingMonitor,
            'direct_marketing_daily_report'=> $directMarketingDailyReport,
            'direct_marketing_aux_inverter_shelly'=> $data['direct_marketing_aux_inverter_shelly'] ?? null,
            'market_value_solar'=> $marketValueSolar,
            'ts'              => filemtime($storagePlanFile),
        ];
        foreach ([
            'pv_forecast_kwh' => 'storage_pv_forecast_kwh',
            'adaptive_headroom_required_wh' => 'storage_adaptive_headroom_required_wh',
            'adaptive_headroom_available_wh' => 'storage_adaptive_headroom_available_wh',
            'adaptive_headroom_need_without_buffer_wh' => 'storage_adaptive_headroom_need_without_buffer_wh',
            'adaptive_headroom_buffer_wh' => 'storage_adaptive_headroom_buffer_wh',
            'adaptive_soc_floor' => 'storage_adaptive_soc_floor',
            'adaptive_soc_ceiling' => 'storage_adaptive_soc_ceiling',
            'adaptive_soc_ceiling_raw' => 'storage_adaptive_soc_ceiling_raw',
            'adaptive_headroom_floor_conflict_points' => 'storage_adaptive_headroom_floor_conflict_points',
            'adaptive_headroom_floor_conflict_max_delta_pct' => 'storage_adaptive_headroom_floor_conflict_max_delta_pct',
            'published_curve_anchor_clamps' => 'storage_published_curve_anchor_clamps',
            'published_curve_timeline_clamps' => 'storage_published_curve_timeline_clamps',
            'published_curve_max_lift_pct' => 'storage_published_curve_max_lift_pct',
            'headroom_reserve_pressure_wh' => 'storage_headroom_reserve_pressure_wh',
            'headroom_reserve_live_pv_w' => 'storage_headroom_reserve_live_pv_w',
            'headroom_reserve_forecast_now_w' => 'storage_headroom_reserve_forecast_now_w',
            'headroom_reserve_forecast_ratio' => 'storage_headroom_reserve_forecast_ratio',
            'headroom_reserve_max_pv_w' => 'storage_headroom_reserve_max_pv_w',
            'headroom_reserve_floor_protected_points' => 'storage_headroom_reserve_floor_protected_points',
            'headroom_reserve_floor_protected_max_delta_pct' => 'storage_headroom_reserve_floor_protected_max_delta_pct',
            'curtailment_pressure_wh' => 'storage_curtailment_pressure_wh',
            'curtailment_unavoidable_wh' => 'storage_curtailment_unavoidable_wh',
            'curtailment_first_pressure_ts' => 'storage_curtailment_first_pressure_ts',
            'curtailment_soc_at_first_pressure' => 'storage_curtailment_soc_at_first_pressure',
            'latest_charge_start_ts' => 'storage_latest_charge_start_ts',
            'evening_shortfall_wh' => 'storage_evening_shortfall_wh',
            'adaptive_storage_kwh' => 'storage_adaptive_storage_kwh',
            'adaptive_large_storage_threshold_kwh' => 'storage_adaptive_large_storage_threshold_kwh',
            'adaptive_comfort_soc' => 'storage_adaptive_comfort_soc',
            'adaptive_comfort_floor_soc' => 'storage_adaptive_comfort_floor_soc',
        ] as $metaKey => $dstKey) {
            if (!isset($data[$dstKey]) && isset($data['storage_plan_meta'][$metaKey]) && is_numeric($data['storage_plan_meta'][$metaKey])) {
                $data[$dstKey] = $data['storage_plan_meta'][$metaKey] + 0;
            }
        }
        foreach ([
            'adaptive_storage_class' => 'storage_adaptive_storage_class',
            'adaptive_comfort_active' => 'storage_adaptive_comfort_active',
            'adaptive_comfort_limited_by_headroom' => 'storage_adaptive_comfort_limited_by_headroom',
            'adaptive_headroom_floor_conflict' => 'storage_adaptive_headroom_floor_conflict',
            'published_curve_floor_active' => 'storage_published_curve_floor_active',
            'published_curve_floor_reset_allowed' => 'storage_published_curve_floor_reset_allowed',
            'headroom_reserve_floor_protected' => 'storage_headroom_reserve_floor_protected',
            'published_curve_floor_policy' => 'storage_published_curve_floor_policy',
            'published_curve_floor_source' => 'storage_published_curve_floor_source',
            'published_curve_floor_reason' => 'storage_published_curve_floor_reason',
            'published_curve_floor_reset_reason' => 'storage_published_curve_floor_reset_reason',
            'headroom_floor_policy' => 'storage_headroom_floor_policy',
        ] as $metaKey => $dstKey) {
            if (!isset($data[$dstKey]) && array_key_exists($metaKey, $data['storage_plan_meta'])) {
                $data[$dstKey] = $data['storage_plan_meta'][$metaKey];
            } elseif (!isset($data[$dstKey]) && array_key_exists($metaKey, $targetCurveMeta)) {
                $data[$dstKey] = $targetCurveMeta[$metaKey];
            }
        }
    }
}

// --- Preis-Boost: EPEX-Fenster und Verbraucher-Freigaben für Frontend ---
$priceBoostPlanFile = '/var/www/html/ramdisk/price_boost_plan.json';
if (file_exists($priceBoostPlanFile) && (time() - filemtime($priceBoostPlanFile) < 86400)) {
    $pbPlan = @json_decode(file_get_contents($priceBoostPlanFile), true);
    if ($pbPlan && is_array($pbPlan)) {
        $nowMs = time() * 1000;
        $activeWin = (isset($pbPlan['active_window']) && is_array($pbPlan['active_window'])) ? $pbPlan['active_window'] : null;
        $activeStart = (int)($activeWin['start_timestamp'] ?? 0);
        $activeEnd = (int)($activeWin['end_timestamp'] ?? 0);
        $boostActiveNow = !empty($pbPlan['active']) && $activeStart <= $nowMs && $nowMs < $activeEnd;
        $data['cheap_grid_boost_enabled'] = !empty($pbPlan['enabled']);
        $data['cheap_grid_boost_active'] = $boostActiveNow;
        $data['cheap_grid_boost_allow'] = (isset($pbPlan['allow']) && is_array($pbPlan['allow'])) ? $pbPlan['allow'] : [];
        $data['cheap_grid_boost_window'] = $boostActiveNow ? $activeWin : null;
        foreach (($pbPlan['windows'] ?? []) as $win) {
            $end = (int)($win['end_timestamp'] ?? 0);
            if ($end > $nowMs) {
                $data['cheap_grid_boost_next_window'] = $win;
                break;
            }
        }
        $data['cheap_grid_boost_price_limit_ct'] = $pbPlan['price_limit_ct'] ?? null;
        $data['cheap_grid_boost_plan_ts'] = $pbPlan['ts'] ?? null;
    }
}

// --- Watchdog Warning (Failsafe) ---
$watchdogWarningFile = '/var/www/html/ramdisk/watchdog_warning.json';
if (file_exists($watchdogWarningFile)) {
    $wdData = @json_decode(file_get_contents($watchdogWarningFile), true);
    // Nur anzeigen, wenn die Datei jünger als z.B. 24h ist (verhindert ewig klebende alte Warnungen falls vergessen zu löschen)
    if ($wdData && isset($wdData['ts']) && (time() - $wdData['ts'] < 86400)) {
        $data['system_warning'] = $wdData['warning'] ?? 'Kerndienst ausgefallen!';
    }
}

// --- Wetter-/Gewitterwarnungen (DWD CAP + Open-Meteo ICON Risiko) ---
$weatherAlertsFile = '/var/www/html/ramdisk/weather_alerts.json';
if (file_exists($weatherAlertsFile)) {
    $weatherAlert = @json_decode(file_get_contents($weatherAlertsFile), true);
    if (is_array($weatherAlert)) {
        $fetchedTs = isset($weatherAlert['fetched_at']) ? strtotime((string)$weatherAlert['fetched_at']) : 0;
        $weatherAlert['stale'] = ($fetchedTs <= 0 || (time() - $fetchedTs) > 3 * 3600);
        $weatherEntries = (isset($weatherAlert['alerts']) && is_array($weatherAlert['alerts'])) ? $weatherAlert['alerts'] : [];
        $weatherRisk = (isset($weatherAlert['risk']) && is_array($weatherAlert['risk'])) ? $weatherAlert['risk'] : [];
        $weatherHighest = (int)($weatherAlert['highest_level'] ?? ($weatherRisk['level'] ?? 0));
        if (!empty($weatherEntries) || !empty($weatherRisk['active']) || $weatherHighest > 0 || !empty($weatherAlert['thunderstorm_active'])) {
            $weatherAlert['active'] = true;
        }
        if (empty($weatherAlert['thunderstorm_active'])) {
            $weatherEncoded = json_encode($weatherAlert, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
            $weatherText = is_string($weatherEncoded) ? strtolower($weatherEncoded) : '';
            if (strpos($weatherText, 'gewitter') !== false || strpos($weatherText, 'thunder') !== false || strpos($weatherText, 'convective') !== false || strpos($weatherText, 'hagel') !== false) {
                $weatherAlert['thunderstorm_active'] = true;
            }
        }
        $storagePlanFile = '/var/www/html/ramdisk/storage_plan.json';
        if (file_exists($storagePlanFile)) {
            $storagePlan = @json_decode(file_get_contents($storagePlanFile), true);
            $stormGuard = is_array($storagePlan['target_curve_meta']['storm_guard'] ?? null)
                ? $storagePlan['target_curve_meta']['storm_guard']
                : null;
            if (is_array($stormGuard)) {
                $weatherAlert['storm_guard'] = [
                    'mode' => $stormGuard['mode'] ?? null,
                    'active' => !empty($stormGuard['active']),
                    'control_active' => !empty($stormGuard['control_active']),
                    'grid_allowed' => !empty($stormGuard['grid_allowed']),
                    'level' => $stormGuard['level'] ?? null,
                    'action_label' => $stormGuard['action_label'] ?? null,
                    'control_summary' => $stormGuard['control_summary'] ?? ($stormGuard['reason'] ?? null),
                ];
                if (!empty($weatherAlert['active']) && empty($stormGuard['control_active']) && empty($stormGuard['grid_allowed'])) {
                    $stormSummary = trim((string)($stormGuard['control_summary'] ?? $stormGuard['reason'] ?? ''));
                    $prefix = 'Warnung beobachtet, kein aktiver Eingriff';
                    $weatherAlert['control_summary'] = $stormSummary !== '' ? $stormSummary : $prefix;
                    $summary = trim((string)($weatherAlert['summary'] ?? ''));
                    if ($summary === '' || strpos($summary, $prefix) === false) {
                        $weatherAlert['summary'] = $prefix . ($summary !== '' ? ': ' . $summary : '');
                    }
                }
            }
        }
        $data['weather_alert'] = $weatherAlert;
    }
}

$data['vehicle_soc_contract_version'] = 1;
if (!isset($data['vehicle_soc']) || !is_array($data['vehicle_soc'])) $data['vehicle_soc'] = [];
if (!isset($data['house_battery_soc']) || !is_array($data['house_battery_soc'])) {
    $data['house_battery_soc'] = [
        'value' => isset($data['soc']) && is_numeric($data['soc']) ? (float)$data['soc'] : null,
        'source' => $data['live_source'] ?? null,
        'source_ts' => null,
        'age_s' => null,
        'domain' => 'house_battery',
    ];
}

// Session-/Legacypfade laufen für Bestandsmigration weiter, dürfen aber am
// Ausgang keinen ausdrücklich deaktivierten Wallbox-Slot wieder projizieren.
e3dcApplyWallboxPresenceProjection($data, $wbConfigured, $wb2Configured, $wb2ExplicitlyDisabled);

echo json_encode($data);
