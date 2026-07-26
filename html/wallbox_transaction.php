<?php
/**
 * Transaktionale Grenze für Wallboxkonfiguration und -planung.
 *
 * Der Planer läuft zuerst in einem privaten Kandidatenverzeichnis. Installiert
 * wird nur ein validierter Konfigurations-/Plansatz mit exakten Byte-Snapshots
 * und Rollback. Das Modul ruft nie eine Shell auf und startet oder stoppt keinen
 * Wärmepumpendienst.
 */

if (!function_exists('e3dcRunArgvProcess')) {
    require_once __DIR__ . '/helpers.php';
}

const E3DC_WB_TX_SCHEMA = 'wallbox_plan_candidate_v1';
const E3DC_WB_TX_RESULT_SCHEMA = 'wallbox_plan_candidate_result_v1';
const E3DC_WB_TX_PLAN_FILES = [
    'native_wallbox_schedule_wb1.json',
    'native_wallbox_schedule_wb2.json',
    'native_wallbox_schedule.json',
];

function e3dcWbTxResult($success, $code, $message, array $extra = []) {
    return array_merge([
        'success' => (bool)$success,
        'code' => (string)$code,
        'message' => (string)$message,
        'rollback_failed' => false,
        'planner' => null,
        'transaction_id' => '',
        'canonical_committed' => false,
        'legacy_projection_status' => 'not_requested',
        'legacy_cleanup_status' => 'not_requested',
    ], $extra);
}

function e3dcWbTxPlannerFailureMessage($resultPath) {
    if (!is_string($resultPath) || $resultPath === '' || is_link($resultPath) || !is_file($resultPath)) {
        return null;
    }
    $size = @filesize($resultPath);
    if ($size === false || $size < 2 || $size > 65536) {
        return null;
    }
    $raw = @file_get_contents($resultPath);
    $result = $raw === false ? null : json_decode($raw, true);
    if (!is_array($result)
        || ($result['schema'] ?? '') !== E3DC_WB_TX_RESULT_SCHEMA
        || !array_key_exists('success', $result)
        || !empty($result['success'])
        || !is_string($result['error'] ?? null)) {
        return null;
    }
    $error = trim((string)$result['error']);
    if ($error === '' || strlen($error) > 256 || !preg_match('/^[a-z0-9_]+$/', $error)) {
        return null;
    }

    if ($error === 'candidate_market_data_missing') {
        return 'Für den dynamischen Tarif fehlen gültige zukünftige Preisdaten. Die Ladeplanung wurde nicht gespeichert.';
    }
    if (preg_match('/^candidate_required_plan_empty_wb([12])$/', $error, $match)) {
        return 'Für Wallbox ' . $match[1] . ' stehen im gewählten Ladefenster nicht genügend gültige Tarif- oder Preisslots bereit. Die Ladeplanung wurde nicht gespeichert.';
    }
    if (str_starts_with($error, 'candidate_config_invalid_')
        || str_starts_with($error, 'candidate_required_plan_')
        || $error === 'candidate_required_plan_mismatch') {
        return 'Die eingegebenen Ladeplanwerte sind ungültig oder widersprüchlich. Die Ladeplanung wurde nicht gespeichert.';
    }
    if (str_starts_with($error, 'candidate_plan_')
        || str_starts_with($error, 'candidate_combined_plan_')) {
        return 'Der erzeugte Ladeplan hat die Sicherheitsprüfung nicht bestanden. Die Ladeplanung wurde nicht gespeichert.';
    }
    if (str_starts_with($error, 'candidate_')) {
        return 'Der Wallbox-Planer konnte aus den Eingaben keinen sicheren Ladeplan erzeugen. Es wurde nichts gespeichert.';
    }
    return null;
}

function e3dcWbTxIsTest(array $options) {
    return PHP_SAPI === 'cli' && !empty($options['test_mode']);
}

function e3dcWbTxFlattenConfig(array $data) {
    $flat = isset($data['config']) && is_array($data['config']) ? $data['config'] : [];
    foreach ($data as $key => $value) {
        if ($key !== 'config') $flat[$key] = $value;
    }
    return $flat;
}

function e3dcWbTxConfiguredContext(array $config, array $options = []) {
    $test = e3dcWbTxIsTest($options);
    $flat = e3dcWbTxFlattenConfig($config);
    $configPath = $test ? (string)($options['config_path'] ?? '') : '/var/www/html/data/e3dc_v4.json';
    $ramdiskDir = $test ? (string)($options['ramdisk_dir'] ?? '') : '/var/www/html/ramdisk';
    $tmpDir = $test ? (string)($options['tmp_dir'] ?? '') : '/var/www/html/tmp';
    $installRoot = $test
        ? (string)($options['install_root'] ?? '')
        : rtrim((string)($flat['install_path'] ?? ''), '/');
    $planner = $test
        ? (string)($options['planner_script'] ?? '')
        : $installRoot . '/Installer/wallbox_planer.py';
    $python = $test
        ? (string)($options['python'] ?? '')
        : (string)(e3dcGetTrustedPythonInterpreter() ?? '');

    foreach ([$configPath, $ramdiskDir, $tmpDir, $installRoot, $planner, $python] as $value) {
        if ($value === '' || !str_starts_with($value, '/')) {
            return ['success' => false, 'error' => 'Unvollständiger, nicht absoluter Laufzeitkontext.'];
        }
    }
    if (!is_file($configPath) || is_link($configPath) || !is_readable($configPath)) {
        return ['success' => false, 'error' => 'V4-Konfiguration ist nicht eindeutig lesbar.'];
    }
    if (!is_dir($ramdiskDir) || is_link($ramdiskDir) || is_link($tmpDir)) {
        return ['success' => false, 'error' => 'RAM-Verzeichnis ist nicht eindeutig.'];
    }
    if (!is_dir($installRoot) || is_link($installRoot)) {
        return ['success' => false, 'error' => 'Installations-Root ist nicht eindeutig.'];
    }
    $realRoot = @realpath($installRoot);
    $realPlanner = @realpath($planner);
    if (
        $realRoot === false || $realPlanner === false || is_link($planner)
        || !is_file($realPlanner) || !is_readable($realPlanner)
        || !str_starts_with($realPlanner, rtrim($realRoot, '/') . '/Installer/')
    ) {
        return ['success' => false, 'error' => 'Wallbox-Planer liegt nicht im validierten Installations-Root.'];
    }
    if (!is_file($python) || !is_executable($python)) {
        return ['success' => false, 'error' => 'Validierter Python-Interpreter fehlt.'];
    }
    $dataDir = dirname($configPath);
    $jobRoot = $test
        ? (string)($options['job_root'] ?? ($dataDir . '/.wallbox_plan_jobs'))
        : $dataDir . '/.wallbox_plan_jobs';
    if (!is_dir($jobRoot)) {
        $oldUmask = umask(0077);
        $made = @mkdir($jobRoot, 0700, false);
        umask($oldUmask);
        if (!$made && !is_dir($jobRoot)) {
            return ['success' => false, 'error' => 'Privates Planner-Jobverzeichnis konnte nicht angelegt werden.'];
        }
    }
    if (is_link($jobRoot) || !@chmod($jobRoot, 0700)) {
        return ['success' => false, 'error' => 'Planner-Jobverzeichnis ist nicht privat.'];
    }
    $mode = @fileperms($jobRoot);
    if ($mode === false || (($mode & 0777) !== 0700)) {
        return ['success' => false, 'error' => 'Planner-Jobverzeichnis besitzt nicht Modus 0700.'];
    }
    return [
        'success' => true,
        'config_path' => $configPath,
        'ramdisk_dir' => rtrim($ramdiskDir, '/'),
        'tmp_dir' => rtrim($tmpDir, '/'),
        'install_root' => rtrim($realRoot, '/'),
        'planner_script' => $realPlanner,
        'python' => $python,
        'job_root' => rtrim($jobRoot, '/'),
        'lock_path' => rtrim($jobRoot, '/') . '/.transaction.lock',
        'cache_path' => rtrim($ramdiskDir, '/') . '/e3dc_config_cache.json',
        'test' => $test,
    ];
}

function e3dcWbTxSnapshot($path, $maxBytes = 16777216) {
    if (!file_exists($path) && !is_link($path)) {
        return [
            'path' => $path, 'exists' => false, 'bytes' => null, 'mode' => null,
            'size' => 0, 'mtime' => null, 'inode' => null,
        ];
    }
    if (is_link($path) || !is_file($path)) {
        throw new RuntimeException('Transaktionsziel ist keine reguläre Datei: ' . basename($path));
    }
    $st = @stat($path);
    if (!is_array($st) || (int)$st['nlink'] !== 1 || (int)$st['size'] > $maxBytes) {
        throw new RuntimeException('Transaktionsziel ist nicht eindeutig oder zu groß: ' . basename($path));
    }
    $bytes = @file_get_contents($path);
    if ($bytes === false || strlen($bytes) !== (int)$st['size']) {
        throw new RuntimeException('Transaktionsziel konnte nicht vollständig gelesen werden: ' . basename($path));
    }
    clearstatcache(true, $path);
    $after = @stat($path);
    if (!is_array($after) || (int)$after['ino'] !== (int)$st['ino'] || (int)$after['size'] !== (int)$st['size'] || (int)$after['mtime'] !== (int)$st['mtime']) {
        throw new RuntimeException('Transaktionsziel wurde beim Lesen verändert: ' . basename($path));
    }
    return [
        'path' => $path,
        'exists' => true,
        'bytes' => $bytes,
        'mode' => ((int)$st['mode']) & 0777,
        'size' => (int)$st['size'],
        'mtime' => (int)$st['mtime'],
        'inode' => (int)$st['ino'],
    ];
}

function e3dcWbTxSnapshotUnchanged(array $snapshot) {
    $path = $snapshot['path'];
    if (empty($snapshot['exists'])) return !file_exists($path) && !is_link($path);
    if (is_link($path) || !is_file($path)) return false;
    $st = @stat($path);
    if (!is_array($st)) return false;
    if ((int)$st['ino'] !== (int)$snapshot['inode']
        || (int)$st['size'] !== (int)$snapshot['size']
        || (int)$st['mtime'] !== (int)$snapshot['mtime']) {
        return false;
    }
    $current = @file_get_contents($path);
    return $current !== false && hash_equals(hash('sha256', (string)$snapshot['bytes']), hash('sha256', $current));
}

function e3dcWbTxAtomicWrite($path, $bytes, $mode) {
    $dir = dirname($path);
    if (!is_dir($dir) || is_link($dir) || file_exists($path) && is_link($path)) return false;
    try {
        $suffix = bin2hex(random_bytes(12));
    } catch (Throwable $e) {
        return false;
    }
    $tmp = $dir . '/.wbtx-' . $suffix . '.tmp';
    $oldUmask = umask(0077);
    $handle = @fopen($tmp, 'x+b');
    umask($oldUmask);
    if ($handle === false) return false;
    $ok = @chmod($tmp, 0600);
    $length = strlen((string)$bytes);
    $written = 0;
    while ($ok && $written < $length) {
        $count = @fwrite($handle, substr($bytes, $written));
        if ($count === false || $count <= 0) {
            $ok = false;
            break;
        }
        $written += $count;
    }
    if ($ok) $ok = @fflush($handle);
    @fclose($handle);
    if ($ok) $ok = @chmod($tmp, (int)$mode);
    if ($ok) $ok = @rename($tmp, $path);
    if (!$ok) @unlink($tmp);
    return $ok;
}

function e3dcWbTxApply($path, $bytes, $mode) {
    if ($bytes === null) {
        if (!file_exists($path) && !is_link($path)) return true;
        return !is_link($path) && is_file($path) && @unlink($path);
    }
    return e3dcWbTxAtomicWrite($path, (string)$bytes, (int)$mode);
}

function e3dcWbTxRestore(array $snapshot) {
    return e3dcWbTxApply(
        $snapshot['path'],
        !empty($snapshot['exists']) ? $snapshot['bytes'] : null,
        !empty($snapshot['exists']) ? (int)$snapshot['mode'] : 0600
    );
}

function e3dcWbTxPrivateJson($path, $data) {
    $json = json_encode($data, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT);
    if ($json === false) return false;
    return e3dcWbTxAtomicWrite($path, $json . "\n", 0600);
}

function e3dcWbTxCleanupJob($jobDir) {
    if (!is_dir($jobDir) || is_link($jobDir) || !preg_match('/\/tx-[a-f0-9]{32}$/', str_replace('\\', '/', $jobDir))) return false;
    $ok = true;
    $items = @scandir($jobDir);
    if (!is_array($items)) return false;
    foreach ($items as $name) {
        if ($name === '.' || $name === '..') continue;
        $path = $jobDir . '/' . $name;
        if (is_link($path) || !is_file($path) || !@unlink($path)) $ok = false;
    }
    return $ok && @rmdir($jobDir);
}

function e3dcWbTxCleanupLegacyArtifacts(array $paths) {
    $paths = array_values(array_unique(array_filter(array_map('strval', $paths))));
    if (empty($paths)) {
        return ['status' => 'not_requested', 'artifacts' => []];
    }
    $artifacts = [];
    $failed = false;
    foreach ($paths as $path) {
        $name = basename($path);
        if (!file_exists($path) && !is_link($path)) {
            $artifacts[] = ['name' => $name, 'status' => 'already_absent'];
            continue;
        }
        if (is_link($path) || !is_file($path)) {
            $artifacts[] = ['name' => $name, 'status' => 'unsafe_target'];
            $failed = true;
            continue;
        }
        if (@unlink($path)) {
            $artifacts[] = ['name' => $name, 'status' => 'deleted'];
        } else {
            $artifacts[] = ['name' => $name, 'status' => 'delete_failed'];
            $failed = true;
        }
    }
    return [
        'status' => $failed ? 'partial_failure' : 'complete',
        'artifacts' => $artifacts,
    ];
}

function e3dcWbTxProjectLegacyArtifacts(array $artifacts, array $options = []) {
    if (empty($artifacts)) {
        return ['status' => 'not_requested', 'artifacts' => []];
    }
    $outcomes = [];
    $failed = false;
    foreach ($artifacts as $artifact) {
        $path = (string)($artifact['path'] ?? '');
        $name = basename($path);
        $mode = (int)($artifact['mode'] ?? 0664);
        $injectedFailure = e3dcWbTxIsTest($options)
            && isset($options['fail_legacy_projection_at'])
            && (string)$options['fail_legacy_projection_at'] === $name;
        if ($path === '' || is_link($path) || $injectedFailure
            || !e3dcWbTxAtomicWrite($path, (string)($artifact['bytes'] ?? ''), $mode)) {
            $outcomes[] = ['name' => $name, 'status' => $injectedFailure ? 'injected_failure' : 'write_failed'];
            $failed = true;
            continue;
        }
        $outcomes[] = ['name' => $name, 'status' => 'projected'];
    }
    return [
        'status' => $failed ? 'partial_failure' : 'complete',
        'artifacts' => $outcomes,
    ];
}

function e3dcWbTxTruthy($value) {
    if (is_bool($value)) return $value;
    if ($value === null || $value === '') return false;
    if (is_numeric($value)) return (float)$value !== 0.0;
    return in_array(strtolower(trim((string)$value)), ['1', 'true', 'yes', 'on'], true);
}

function e3dcWbTxManualPlanRequired(array $flat, $wbId) {
    $modeKey = 'wb' . $wbId . '_mode';
    if (array_key_exists($modeKey, $flat) && trim((string)$flat[$modeKey]) !== '' && (int)$flat[$modeKey] === 0) return false;
    $legacy = $wbId === 1 ? ($flat['wbhour'] ?? $flat['Wbhour'] ?? 0) : 0;
    $hours = (int)($flat['wb' . $wbId . '_plan_hours'] ?? $flat['wb' . $wbId . '_wbhour'] ?? $legacy);
    $legacySofort = $wbId === 1 ? ($flat['wb_sofort'] ?? '0') : '0';
    $sofort = e3dcWbTxTruthy($flat['wb' . $wbId . '_sofort'] ?? $legacySofort);
    return $hours > 0 || $sofort;
}

function e3dcWbTxValidateUpdates(array $updates) {
    $clean = [];
    foreach ($updates as $key => $value) {
        $key = strtolower(trim((string)$key));
        if ($key === '' || !preg_match('/^[a-z0-9_]{1,128}$/', $key)) {
            throw new InvalidArgumentException('Ungültiger Konfigurationsschlüssel.');
        }
        if (!is_scalar($value) || strpos((string)$value, "\0") !== false || strlen((string)$value) > 65536) {
            throw new InvalidArgumentException('Ungültiger Konfigurationswert für ' . $key . '.');
        }
        $clean[$key] = is_string($value) ? trim($value) : $value;
    }
    return $clean;
}

function e3dcWbTxSavedCarsBytes($payload) {
    if (!is_array($payload) || !array_is_list($payload) || count($payload) > 512) {
        throw new InvalidArgumentException('Fahrzeugprofil-Kandidat muss ein begrenztes JSON-Array sein.');
    }
    foreach ($payload as $car) {
        if (!is_array($car)) {
            throw new InvalidArgumentException('Fahrzeugprofil-Kandidat enthält einen ungültigen Eintrag.');
        }
        foreach ($car as $key => $value) {
            if (!is_string($key) || strlen($key) > 128 || is_array($value) || is_object($value) || is_resource($value)) {
                throw new InvalidArgumentException('Fahrzeugprofil-Kandidat enthält ein ungültiges Feld.');
            }
            if (is_string($value) && (strpos($value, "\0") !== false || strlen($value) > 65536)) {
                throw new InvalidArgumentException('Fahrzeugprofil-Kandidat enthält einen ungültigen Wert.');
            }
        }
    }
    $json = json_encode($payload, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT);
    if ($json === false || strlen($json) > 1048576) {
        throw new InvalidArgumentException('Fahrzeugprofil-Kandidat ist nicht sicher kodierbar.');
    }
    return $json . "\n";
}

function e3dcWbTxModeRequestBytes(array $snapshot, $wbId, $newMode, $oldMode, $kind) {
    $data = [];
    if (!empty($snapshot['exists'])) {
        $decoded = json_decode((string)$snapshot['bytes'], true);
        if (!is_array($decoded)) throw new RuntimeException('Vorhandene Wallbox-Anforderungsdatei ist ungültig.');
        $data = $decoded;
    }
    $key = (string)max(1, min(2, (int)$wbId));
    if ($kind === 'default') {
        $data[$key] = [
            'ts' => time(), 'source' => 'Wallbox.php', 'reason' => 'mode0_user_switch',
            'previous_mode' => (string)$oldMode,
        ];
    } else {
        $data[$key] = [
            'ts' => time(), 'source' => 'Wallbox.php', 'reason' => 'mode2_user_switch_pv',
            'target_mode' => '2', 'previous_mode' => (string)$oldMode,
        ];
    }
    $json = json_encode($data, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    if ($json === false) throw new RuntimeException('Wallbox-Anforderung konnte nicht kodiert werden.');
    return $json . "\n";
}

function e3dcWallboxPlanTransaction(array $updates, array $options = []) {
    $txId = '';
    $jobDir = '';
    $lock = null;
    $mutated = [];
    $snapshots = [];
    $context = null;
    try {
        if (array_key_exists('sync_legacy_config', $options) && !empty($options['sync_legacy_config'])) {
            return e3dcWbTxResult(
                false,
                'legacy_projection_unsupported',
                'Der kanonische V4-Commit unterstützt keinen gekoppelten Legacy-Spiegel.',
                ['legacy_projection_status' => 'separate_projection_required']
            );
        }
        $updates = e3dcWbTxValidateUpdates($updates);
        $configPath = e3dcWbTxIsTest($options)
            ? (string)($options['config_path'] ?? '')
            : '/var/www/html/data/e3dc_v4.json';
        if ($configPath === '' || !is_file($configPath) || is_link($configPath)) {
            return e3dcWbTxResult(false, 'config_missing', 'V4-Konfiguration fehlt oder ist nicht eindeutig.');
        }
        $rawConfigBytes = @file_get_contents($configPath);
        $rawConfig = $rawConfigBytes === false ? null : json_decode($rawConfigBytes, true);
        if (!is_array($rawConfig)) {
            return e3dcWbTxResult(false, 'config_invalid', 'V4-Konfiguration ist nicht lesbar.');
        }
        $context = e3dcWbTxConfiguredContext($rawConfig, $options);
        if (empty($context['success'])) {
            return e3dcWbTxResult(false, 'context_invalid', $context['error'] ?? 'Laufzeitkontext ist ungültig.');
        }
        $savedCarsRequested = array_key_exists('saved_cars', $options);
        $savedCarsBytes = $savedCarsRequested ? e3dcWbTxSavedCarsBytes($options['saved_cars']) : null;
        $savedCarsExpectedRevision = $savedCarsRequested
            ? strtolower(trim((string)($options['expected_saved_cars_sha256'] ?? '')))
            : '';
        if ($savedCarsRequested && $savedCarsExpectedRevision !== 'absent'
            && !preg_match('/^[a-f0-9]{64}$/', $savedCarsExpectedRevision)) {
            return e3dcWbTxResult(false, 'saved_cars_revision_missing', 'Fahrzeugprofil-Preimage ist nicht eindeutig gebunden.');
        }
        $savedCarsPath = $savedCarsRequested
            ? ($context['test'] ? (string)($options['saved_cars_path'] ?? '') : '/var/www/html/data/saved_cars.json')
            : '';
        if ($savedCarsRequested) {
            $savedCarsDir = dirname($savedCarsPath);
            if ($savedCarsPath === '' || !str_starts_with($savedCarsPath, '/')
                || !is_dir($savedCarsDir) || is_link($savedCarsDir)
                || is_link($savedCarsPath) || file_exists($savedCarsPath) && !is_file($savedCarsPath)) {
                return e3dcWbTxResult(false, 'saved_cars_context_invalid', 'Fahrzeugprofil-Speicher ist nicht eindeutig.');
            }
        }

        $lock = @fopen($context['lock_path'], 'c+b');
        if ($lock === false || !@chmod($context['lock_path'], 0600)) {
            return e3dcWbTxResult(false, 'lock_open_failed', 'Transaktionssperre konnte nicht geöffnet werden.');
        }
        $lockDeadline = microtime(true) + max(0.1, min(10.0, (float)($options['lock_timeout'] ?? 2.0)));
        $locked = false;
        do {
            $locked = @flock($lock, LOCK_EX | LOCK_NB);
            if (!$locked) usleep(20000);
        } while (!$locked && microtime(true) < $lockDeadline);
        if (!$locked) {
            return e3dcWbTxResult(false, 'lock_busy', 'Eine zweite Wallbox-Transaktion ist bereits aktiv.');
        }

        // Unter der Sperre erneut lesen. Ein zweiter Auftrag darf nie auf einer stale Basis aufbauen.
        $rawConfigBytes = @file_get_contents($context['config_path']);
        $rawConfig = $rawConfigBytes === false ? null : json_decode($rawConfigBytes, true);
        if (!is_array($rawConfig)) throw new RuntimeException('Konfiguration unter Sperre nicht lesbar.');
        $candidate = $rawConfig;
        foreach ($updates as $key => $value) $candidate[$key] = $value;

        $operation = (string)($options['operation'] ?? 'plan');
        if (!in_array($operation, ['plan', 'clear', 'preserve'], true)) {
            throw new InvalidArgumentException('Ungültige Wallbox-Transaktionsart.');
        }
        $abortAction = (string)($options['abort_flag'] ?? 'preserve');
        $emergencyAction = (string)($options['emergency_flag'] ?? 'preserve');
        if (!in_array($abortAction, ['preserve', 'remove', 'create'], true)
            || !in_array($emergencyAction, ['preserve', 'remove', 'create'], true)) {
            throw new InvalidArgumentException('Ungültige Flag-Transaktion.');
        }

        $txId = bin2hex(random_bytes(16));
        $jobDir = $context['job_root'] . '/tx-' . $txId;
        $oldUmask = umask(0077);
        $made = @mkdir($jobDir, 0700, false);
        umask($oldUmask);
        if (!$made || !@chmod($jobDir, 0700)) throw new RuntimeException('Privater Planner-Job konnte nicht angelegt werden.');

        $ramdisk = $context['ramdisk_dir'];
        $planTargets = [];
        foreach (E3DC_WB_TX_PLAN_FILES as $filename) $planTargets[$filename] = $ramdisk . '/' . $filename;
        $abortPath = $ramdisk . '/native_schedule_aborted.flag';
        $emergencyPath = $ramdisk . '/wallbox_emergency_stop.flag';
        $cachePath = $context['cache_path'];
        $targets = [$context['config_path'], $cachePath, ...array_values($planTargets)];
        if ($savedCarsRequested) $targets[] = $savedCarsPath;

        if ($abortAction !== 'preserve') $targets[] = $abortPath;
        if ($emergencyAction !== 'preserve') $targets[] = $emergencyPath;

        $manualSoc = isset($options['manual_soc']) && is_array($options['manual_soc']) ? $options['manual_soc'] : [];
        foreach ($manualSoc as $wbId => $payload) {
            $wbId = max(1, min(2, (int)$wbId));
            $targets[] = $ramdisk . '/manual_soc_wb' . $wbId . '.json';
        }

        $modeTransition = isset($options['mode_transition']) && is_array($options['mode_transition'])
            ? $options['mode_transition'] : null;
        $requestTarget = null;
        $requestKind = null;
        if ($modeTransition) {
            $newMode = (string)($modeTransition['new_mode'] ?? '');
            $oldMode = (string)($modeTransition['old_mode'] ?? '');
            if ($newMode === '0' && $oldMode !== '0') {
                $requestKind = 'default';
                $requestTarget = $ramdisk . '/wallbox_default_release_request.json';
            } elseif ($newMode === '2' && $oldMode !== '2') {
                $requestKind = 'user';
                $requestTarget = $ramdisk . '/wallbox_user_mode_request.json';
            }
            if ($requestTarget !== null) $targets[] = $requestTarget;
        }

        $legacyDeletes = [];
        if (!empty($options['delete_legacy_schedule'])) {
            $legacyDeletes[] = $context['install_root'] . '/e3dc.wallbox.out';
        }
        if (!empty($options['delete_legacy_wallbox_command'])) {
            $legacyDeletes[] = $context['install_root'] . '/e3dc.wallbox.txt';
        }
        $targets = array_values(array_unique($targets));
        foreach ($targets as $path) $snapshots[$path] = e3dcWbTxSnapshot($path);
        if ($savedCarsRequested && !empty($snapshots[$savedCarsPath]['exists'])) {
            $storedCars = json_decode((string)$snapshots[$savedCarsPath]['bytes'], true);
            if (!is_array($storedCars) || !array_is_list($storedCars)) {
                throw new RuntimeException('Vorhandener Fahrzeugprofil-Speicher ist kein gültiges JSON-Array.');
            }
        }
        if ($savedCarsRequested) {
            $currentSavedCarsRevision = !empty($snapshots[$savedCarsPath]['exists'])
                ? hash('sha256', (string)$snapshots[$savedCarsPath]['bytes'])
                : 'absent';
            if ($currentSavedCarsRevision !== $savedCarsExpectedRevision) {
                return e3dcWbTxResult(false, 'concurrent_change', 'Fahrzeugprofile wurden seit dem Formularaufruf verändert; nichts wurde übernommen.');
            }
        }
        if (!hash_equals(
            hash('sha256', (string)$rawConfigBytes),
            hash('sha256', (string)$snapshots[$context['config_path']]['bytes'])
        )) {
            throw new RuntimeException('Konfiguration wurde vor dem Snapshot verändert.');
        }

        // Kopiert nur die deklarierten read-only Planereingaben in den privaten Lauf.
        $inputNames = [
            'epex_daten.json', 'eco_score.json', 'vehicles.json',
            'bluelink_soc.json', 'car_soc.json',
            ...E3DC_WB_TX_PLAN_FILES,
        ];
        foreach ($inputNames as $name) {
            $source = $ramdisk . '/' . $name;
            if (!is_file($source) || is_link($source)) continue;
            $sourceSnapshot = isset($snapshots[$source]) ? $snapshots[$source] : e3dcWbTxSnapshot($source);
            if (!e3dcWbTxAtomicWrite($jobDir . '/' . $name, $sourceSnapshot['bytes'], 0600)) {
                throw new RuntimeException('Planner-Eingabe konnte nicht privat kopiert werden.');
            }
        }
        foreach ($manualSoc as $wbId => $payload) {
            $wbId = max(1, min(2, (int)$wbId));
            if (!is_array($payload) || !isset($payload['soc']) || !is_numeric($payload['soc'])) {
                throw new InvalidArgumentException('Manueller SoC ist ungültig.');
            }
            $payload['wb'] = $wbId;
            $payload['ts'] = isset($payload['ts']) ? (int)$payload['ts'] : time();
            if (!e3dcWbTxPrivateJson($jobDir . '/manual_soc_wb' . $wbId . '.json', $payload)) {
                throw new RuntimeException('Manueller SoC konnte nicht für die Kandidatenplanung bereitgestellt werden.');
            }
        }

        $flatCandidate = e3dcWbTxFlattenConfig($candidate);
        $required = [];
        foreach ([1, 2] as $wbId) if (e3dcWbTxManualPlanRequired($flatCandidate, $wbId)) $required[] = $wbId;
        if (!e3dcWbTxPrivateJson($jobDir . '/candidate_request.json', [
            'schema' => E3DC_WB_TX_SCHEMA,
            'operation' => $operation,
            'require_plan' => $required,
        ])) throw new RuntimeException('Planner-Auftrag konnte nicht geschrieben werden.');
        if (!e3dcWbTxPrivateJson($jobDir . '/candidate_config.json', $candidate)) {
            throw new RuntimeException('Konfigurationskandidat konnte nicht geschrieben werden.');
        }

        $plannerTimeout = max(1.0, min(120.0, (float)($options['planner_timeout'] ?? 20.0)));
        $planner = e3dcRunArgvProcess(
            [$context['python'], $context['planner_script'], '--candidate-dir', $jobDir],
            $plannerTimeout,
            ['cwd' => dirname($context['planner_script']), 'max_output_bytes' => 65536]
        );
        if (empty($planner['success'])) {
            $detail = !empty($planner['timed_out']) ? 'Timeout' : ((int)($planner['signal'] ?? 0) > 0 ? 'Signal ' . (int)$planner['signal'] : 'rc=' . (int)($planner['exit_code'] ?? 1));
            $safePlannerMessage = e3dcWbTxPlannerFailureMessage($jobDir . '/planner_result.json');
            $message = $safePlannerMessage
                ?? ('Der Wallbox-Planer wurde ohne gültige Fehlerdiagnose beendet (' . $detail . '). Es wurde nichts gespeichert.');
            return e3dcWbTxResult(false, 'planner_failed', $message, [
                'planner' => $planner, 'transaction_id' => $txId,
            ]);
        }
        $resultPath = $jobDir . '/planner_result.json';
        $resultRaw = is_file($resultPath) && !is_link($resultPath) ? @file_get_contents($resultPath) : false;
        $plannerResult = $resultRaw === false ? null : json_decode($resultRaw, true);
        if (!is_array($plannerResult) || empty($plannerResult['success']) || ($plannerResult['schema'] ?? '') !== E3DC_WB_TX_RESULT_SCHEMA) {
            return e3dcWbTxResult(false, 'planner_result_invalid', 'Planner-Ergebnis ist nicht vertrauenswürdig.', [
                'planner' => $planner, 'transaction_id' => $txId,
            ]);
        }
        $candidateConfigPath = $jobDir . '/candidate_config.json';
        $candidateBytes = @file_get_contents($candidateConfigPath);
        if ($candidateBytes === false || !hash_equals((string)($plannerResult['config_sha256'] ?? ''), hash_file('sha256', $candidateConfigPath))) {
            return e3dcWbTxResult(false, 'candidate_hash_mismatch', 'Konfigurationskandidat stimmt nicht mit dem Planner-Manifest überein.', [
                'planner' => $planner, 'transaction_id' => $txId,
            ]);
        }
        foreach (($plannerResult['plans'] ?? []) as $filename => $manifest) {
            if (!in_array($filename, E3DC_WB_TX_PLAN_FILES, true)) {
                return e3dcWbTxResult(false, 'plan_manifest_invalid', 'Planner-Manifest enthält eine unzulässige Datei.', ['planner' => $planner, 'transaction_id' => $txId]);
            }
            $path = $jobDir . '/' . $filename;
            if (!is_file($path) || is_link($path) || !hash_equals((string)($manifest['sha256'] ?? ''), hash_file('sha256', $path))) {
                return e3dcWbTxResult(false, 'plan_hash_mismatch', 'Plan stimmt nicht mit dem Planner-Manifest überein.', ['planner' => $planner, 'transaction_id' => $txId]);
            }
        }

        if ($context['test'] && !empty($options['hold_before_commit_ms'])) {
            usleep(max(0, min(5000, (int)$options['hold_before_commit_ms'])) * 1000);
        }
        foreach ($snapshots as $snapshot) {
            if (!e3dcWbTxSnapshotUnchanged($snapshot)) {
                return e3dcWbTxResult(false, 'concurrent_change', 'Eine Laufzeitdatei wurde während der Planung verändert; nichts wurde übernommen.', [
                    'planner' => $planner, 'transaction_id' => $txId,
                ]);
            }
        }
        $desired = [];
        $legacyProjections = [];
        foreach ($manualSoc as $wbId => $payload) {
            $wbId = max(1, min(2, (int)$wbId));
            $bytes = @file_get_contents($jobDir . '/manual_soc_wb' . $wbId . '.json');
            if ($bytes === false) throw new RuntimeException('Validierter SoC-Kandidat fehlt.');
            $target = $ramdisk . '/manual_soc_wb' . $wbId . '.json';
            $desired[] = [$target, $bytes, !empty($snapshots[$target]['exists']) ? $snapshots[$target]['mode'] : 0664];
            if ($wbId === 1) {
                $legacyTarget = $context['tmp_dir'] . '/manual_soc.json';
                $legacyProjections[] = ['path' => $legacyTarget, 'bytes' => $bytes, 'mode' => 0664];
            }
        }
        foreach ($planTargets as $filename => $target) {
            $candidatePlan = $jobDir . '/' . $filename;
            $bytes = is_file($candidatePlan) && !is_link($candidatePlan) ? @file_get_contents($candidatePlan) : null;
            if ($bytes === false) throw new RuntimeException('Plan-Kandidat ist nicht lesbar.');
            $desired[] = [$target, $bytes, !empty($snapshots[$target]['exists']) ? $snapshots[$target]['mode'] : 0664];
        }
        if ($savedCarsRequested) {
            $desired[] = [
                $savedCarsPath,
                $savedCarsBytes,
                !empty($snapshots[$savedCarsPath]['exists']) ? $snapshots[$savedCarsPath]['mode'] : 0660,
            ];
        }
        $desired[] = [$context['config_path'], $candidateBytes, $snapshots[$context['config_path']]['mode']];
        $desired[] = [$cachePath, null, 0600];
        if ($abortAction === 'remove') $desired[] = [$abortPath, null, 0600];
        if ($abortAction === 'create') $desired[] = [$abortPath, gmdate('c') . "\n", !empty($snapshots[$abortPath]['exists']) ? $snapshots[$abortPath]['mode'] : 0644];
        if ($requestTarget !== null && $requestKind !== null) {
            $desired[] = [
                $requestTarget,
                e3dcWbTxModeRequestBytes(
                    $snapshots[$requestTarget],
                    (int)($modeTransition['wb_id'] ?? 1),
                    (string)($modeTransition['new_mode'] ?? ''),
                    (string)($modeTransition['old_mode'] ?? ''),
                    $requestKind
                ),
                !empty($snapshots[$requestTarget]['exists']) ? $snapshots[$requestTarget]['mode'] : 0664,
            ];
        }
        // Entfernen/Erzeugen des Notfallmarkers ist bewusst der letzte öffentliche Schreibzugriff.
        // Ein früher fehlgeschlagener Auftrag kann deshalb nie eine unverriegelte Wallbox freigeben.
        if ($emergencyAction === 'remove') $desired[] = [$emergencyPath, null, 0600];
        if ($emergencyAction === 'create') $desired[] = [$emergencyPath, gmdate('c') . "\n", !empty($snapshots[$emergencyPath]['exists']) ? $snapshots[$emergencyPath]['mode'] : 0644];

        foreach ($desired as $index => [$path, $bytes, $mode]) {
            if ($context['test'] && isset($options['fail_commit_at']) && (string)$options['fail_commit_at'] === basename($path)) {
                throw new RuntimeException('Injected commit failure');
            }
            if (!e3dcWbTxApply($path, $bytes, $mode)) {
                throw new RuntimeException('Commit fehlgeschlagen: ' . basename($path));
            }
            $mutated[] = $path;
        }
        // Alte C++-Artefakte sind keine kanonische Wahrheit. Ihre Bereinigung
        // läuft bewusst erst nach dem bestätigten V4/Plan/Flag-Commit und kann
        // diesen weder verhindern noch zurückrollen.
        $legacyProjection = e3dcWbTxProjectLegacyArtifacts($legacyProjections, $options);
        $legacyCleanup = e3dcWbTxCleanupLegacyArtifacts($legacyDeletes);
        $projectionFailed = ($legacyProjection['status'] ?? '') === 'partial_failure';
        $cleanupFailed = ($legacyCleanup['status'] ?? '') === 'partial_failure';
        $degraded = $projectionFailed || $cleanupFailed;
        $code = $cleanupFailed
            ? 'committed_legacy_cleanup_failed'
            : ($projectionFailed ? 'committed_legacy_projection_failed' : 'committed');
        return e3dcWbTxResult(true, $code, $degraded
            ? 'Der kanonische Wallbox-Stand wurde übernommen; eine nachgelagerte Legacy-Projektion oder -Bereinigung blieb unvollständig.'
            : 'Wallbox-Konfiguration und Ladeplan wurden gemeinsam übernommen.', [
            'planner' => $planner,
            'transaction_id' => $txId,
            'config_sha256' => hash('sha256', $candidateBytes),
            'saved_cars_sha256' => $savedCarsRequested ? hash('sha256', (string)$savedCarsBytes) : null,
            'plan_manifest' => $plannerResult['plans'] ?? [],
            'canonical_committed' => true,
            'legacy_projection_status' => $legacyProjection['status'] ?? 'not_requested',
            'legacy_projection' => $legacyProjection['artifacts'] ?? [],
            'legacy_cleanup_status' => $legacyCleanup['status'] ?? 'not_requested',
            'legacy_cleanup' => $legacyCleanup['artifacts'] ?? [],
        ]);
    } catch (Throwable $error) {
        $rollbackFailed = false;
        foreach (array_reverse(array_values(array_unique($mutated))) as $path) {
            if (!isset($snapshots[$path])) continue;
            if ($context && !empty($context['test']) && isset($options['fail_rollback_at']) && (string)$options['fail_rollback_at'] === basename($path)) {
                $rollbackFailed = true;
                continue;
            }
            if (!e3dcWbTxRestore($snapshots[$path])) $rollbackFailed = true;
        }
        if ($rollbackFailed && is_array($context) && !empty($context['ramdisk_dir'])) {
            // Bestehende Manager-Semantik: Das vorhandene Flag sperrt alle
            // Wallbox-Aktorfreigaben und fordert wiederholt STOP an.
            e3dcWbTxAtomicWrite(
                $context['ramdisk_dir'] . '/wallbox_emergency_stop.flag',
                "transaction_rollback_failed\n",
                0644
            );
        }
        return e3dcWbTxResult(false, $rollbackFailed ? 'rollback_failed' : 'transaction_failed', $rollbackFailed
            ? 'Transaktion und Rückrollen unvollständig; Wallbox-Aktorschreiben bleiben per NOT-AUS gesperrt.'
            : 'Wallbox-Transaktion fehlgeschlagen; der vorherige Dateistand wurde wiederhergestellt.', [
                'rollback_failed' => $rollbackFailed,
                'transaction_id' => $txId,
                'error' => substr($error->getMessage(), 0, 256),
            ]);
    } finally {
        if ($jobDir !== '') e3dcWbTxCleanupJob($jobDir);
        if (is_resource($lock)) {
            @flock($lock, LOCK_UN);
            @fclose($lock);
        }
    }
}
