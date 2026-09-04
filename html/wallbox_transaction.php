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
    if ($installRoot === '' && function_exists('getInstallPath')) {
        $resolvedInstallPath = (string)getInstallPath();
        if ($resolvedInstallPath !== '') {
            $installRoot = rtrim($resolvedInstallPath, '/');
        }
    }
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
            'size' => 0, 'mtime' => null, 'inode' => null, 'uid' => null,
            'gid' => null,
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
        'uid' => (int)$st['uid'],
        'gid' => (int)$st['gid'],
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
        || (int)$st['mtime'] !== (int)$snapshot['mtime']
        || (int)$st['uid'] !== (int)$snapshot['uid']
        || (int)$st['gid'] !== (int)$snapshot['gid']
        || (((int)$st['mode']) & 0777) !== (int)$snapshot['mode']) {
        return false;
    }
    $current = @file_get_contents($path);
    return $current !== false && hash_equals(hash('sha256', (string)$snapshot['bytes']), hash('sha256', $current));
}

function e3dcWbTxFsyncDirectory($path, $failureStage = '') {
    if (!function_exists('fsync') || $failureStage === 'dir_fsync') return false;
    $handle = @fopen($path, 'r');
    if ($handle === false) return false;
    $ok = @fsync($handle);
    @fclose($handle);
    return $ok;
}

function e3dcWbTxMode5ManagerUidFromUnit(
    $unitPath,
    $expectedUid = 0,
    $expectedGid = 0
) {
    if (!function_exists('posix_getpwnam')) return null;
    $parentPath = dirname($unitPath);
    $parent = @lstat($parentPath);
    if (!is_array($parent)
        || ((((int)$parent['mode']) & 0170000) !== 0040000)
        || is_link($parentPath)
        || (int)$parent['uid'] !== (int)$expectedUid
        || (int)$parent['gid'] !== (int)$expectedGid
        || ((((int)$parent['mode']) & 0022) !== 0)) {
        return null;
    }
    $handle = @fopen($unitPath, 'rb');
    if ($handle === false) return null;
    try {
        $before = @fstat($handle);
        if (!is_array($before)
            || ((((int)$before['mode']) & 0170000) !== 0100000)
            || (int)$before['nlink'] !== 1
            || (int)$before['uid'] !== (int)$expectedUid
            || (int)$before['gid'] !== (int)$expectedGid
            || ((((int)$before['mode']) & 0777) !== 0644)
            || (int)$before['size'] < 2
            || (int)$before['size'] > 65536) {
            return null;
        }
        $raw = stream_get_contents($handle, 65537);
        $after = @fstat($handle);
        clearstatcache(true, $unitPath);
        $named = @lstat($unitPath);
        if (!is_string($raw)
            || strlen($raw) !== (int)$before['size']
            || !is_array($after)
            || !is_array($named)) {
            return null;
        }
        foreach (['dev', 'ino', 'mode', 'nlink', 'uid', 'gid', 'size', 'mtime', 'ctime'] as $key) {
            if ((int)$before[$key] !== (int)$after[$key]
                || (int)$after[$key] !== (int)$named[$key]) {
                return null;
            }
        }
    } finally {
        @fclose($handle);
    }

    $section = '';
    $users = [];
    $groups = [];
    $execStarts = [];
    foreach (preg_split('/\r?\n/', $raw) as $line) {
        $trimmed = trim((string)$line);
        if ($trimmed === '' || $trimmed[0] === '#' || $trimmed[0] === ';') continue;
        if (preg_match('/^\[([^\]]+)\]$/', $trimmed, $match)) {
            $section = strtolower(trim((string)$match[1]));
            continue;
        }
        if ($section !== 'service') continue;
        if (preg_match('/^User=([a-z_][a-z0-9_-]{0,31})$/i', $trimmed, $match)) {
            $users[] = (string)$match[1];
        } elseif (preg_match('/^Group=([a-z_][a-z0-9_-]{0,31})$/i', $trimmed, $match)) {
            $groups[] = (string)$match[1];
        } elseif (strpos($trimmed, 'ExecStart=') === 0) {
            $execStarts[] = substr($trimmed, strlen('ExecStart='));
        }
    }
    if (count($users) !== 1
        || count($groups) !== 1
        || $groups[0] !== 'www-data'
        || count($execStarts) !== 1
        || !preg_match('~(?:^|\s)/[^\r\n\x00]*/Installer/wallbox_manager\.py(?:\s|$)~', $execStarts[0])) {
        return null;
    }
    $account = @posix_getpwnam($users[0]);
    return is_array($account) && isset($account['uid'])
        ? (int)$account['uid']
        : null;
}

function e3dcWbTxMode5AllowedParentUids($path) {
    if (!function_exists('posix_geteuid')) return [];
    $allowed = [(int)posix_geteuid()];
    $managerUid = e3dcWbTxMode5ManagerUidFromUnit(
        '/etc/systemd/system/e3dc-wallbox-manager.service'
    );
    if ($managerUid !== null) $allowed[] = (int)$managerUid;
    return array_values(array_unique($allowed));
}

function e3dcWbTxMode5AllowedLockUids() {
    if (!function_exists('posix_geteuid')) return [];
    $allowed = [0, (int)posix_geteuid()];
    $managerUid = e3dcWbTxMode5ManagerUidFromUnit(
        '/etc/systemd/system/e3dc-wallbox-manager.service'
    );
    if ($managerUid !== null) $allowed[] = (int)$managerUid;
    return array_values(array_unique($allowed));
}

function e3dcWbTxConfiguredDataDirMode($path) {
    $configPath = dirname((string)$path) . '/e3dc_v4.json';
    $raw = e3dcReadRegularFileBound($configPath, 1048576);
    $data = is_string($raw) ? @json_decode($raw, true) : null;
    return e3dcConfigSecretDirModeFromData(is_array($data) ? $data : []);
}

function e3dcWbTxMode5SurfaceContract($path) {
    if (!function_exists('posix_geteuid') || !function_exists('posix_getegid')) return false;
    $dir = dirname($path);
    $parent = @lstat($dir);
    $allowedParentUids = e3dcWbTxMode5AllowedParentUids($path);
    $expectedParentMode = e3dcWbTxConfiguredDataDirMode($path);
    if (!is_array($parent)
        || ((((int)$parent['mode']) & 0170000) !== 0040000)
        || is_link($dir)
        || ((((int)$parent['mode']) & 07777) !== $expectedParentMode)
        || !in_array((int)$parent['uid'], $allowedParentUids, true)
        || (int)$parent['gid'] !== (int)posix_getegid()) {
        return false;
    }
    if (!file_exists($path) && !is_link($path)) {
        return ['uid' => (int)posix_geteuid(), 'gid' => (int)$parent['gid']];
    }
    $target = @lstat($path);
    if (!is_array($target)
        || ((((int)$target['mode']) & 0170000) !== 0100000)
        || (int)$target['nlink'] !== 1
        || (int)$target['size'] < 1
        || (int)$target['size'] > 65536
        || ((((int)$target['mode']) & 0777) !== 0660)
        || (int)$target['uid'] !== (int)posix_geteuid()
        || (int)$target['gid'] !== (int)$parent['gid']) {
        return false;
    }
    return ['uid' => (int)posix_geteuid(), 'gid' => (int)$parent['gid']];
}

function e3dcWbTxAtomicWrite(
    $path,
    $bytes,
    $mode,
    $failureStage = '',
    $strictMode5Surface = false,
    &$publishedMutation = null
) {
    $publishedMutation = false;
    $dir = dirname($path);
    $sharedContract = $strictMode5Surface
        ? e3dcWbTxMode5SurfaceContract($path)
        : null;
    if (!function_exists('fsync')
        || !is_dir($dir)
        || is_link($dir)
        || file_exists($path) && is_link($path)
        || $strictMode5Surface && !is_array($sharedContract)
        || $strictMode5Surface && (((int)$mode & 0777) !== 0660)) return false;
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
    if ($ok) $ok = @chmod($tmp, (int)$mode);
    $opened = $ok ? @fstat($handle) : false;
    if ($ok) {
        $ok = is_array($opened)
            && ((((int)$opened['mode']) & 0170000) === 0100000)
            && (int)$opened['nlink'] === 1
            && (int)$opened['size'] === $length
            && ((((int)$opened['mode']) & 0777) === ((int)$mode & 0777));
    }
    if ($ok && $strictMode5Surface) {
        $ok = (int)$opened['uid'] === (int)$sharedContract['uid']
            && (int)$opened['gid'] === (int)$sharedContract['gid'];
    }
    if ($ok) {
        $ok = $failureStage !== 'file_fsync' && @fsync($handle);
    }
    @fclose($handle);
    if ($ok && $failureStage === 'rename') $ok = false;
    if ($ok) $ok = @rename($tmp, $path);
    if (!$ok) {
        @unlink($tmp);
        return false;
    }
    $publishedMutation = true;
    $published = @lstat($path);
    $ok = is_array($published)
        && ((((int)$published['mode']) & 0170000) === 0100000)
        && (int)$published['nlink'] === 1
        && (int)$published['size'] === $length
        && ((((int)$published['mode']) & 0777) === ((int)$mode & 0777))
        && (int)$published['uid'] === (int)$opened['uid']
        && (int)$published['gid'] === (int)$opened['gid'];
    if ($ok && $strictMode5Surface) {
        $ok = is_array(e3dcWbTxMode5SurfaceContract($path));
    }
    return $ok && e3dcWbTxFsyncDirectory($dir, $failureStage);
}

function e3dcWbTxApply(
    $path,
    $bytes,
    $mode,
    $failureStage = '',
    $strictMode5Surface = false,
    &$publishedMutation = null
) {
    $publishedMutation = false;
    if ($bytes === null) {
        if (!file_exists($path) && !is_link($path)) return true;
        if (is_link($path) || !is_file($path) || !@unlink($path)) return false;
        $publishedMutation = true;
        return e3dcWbTxFsyncDirectory(dirname($path), $failureStage);
    }
    return e3dcWbTxAtomicWrite(
        $path,
        (string)$bytes,
        (int)$mode,
        (string)$failureStage,
        (bool)$strictMode5Surface,
        $publishedMutation
    );
}

function e3dcWbTxAcquireSharedRequestLock(
    $path,
    $timeout = 2.0,
    $mode = 0664,
    $expectedGid = null,
    $allowedOwnerUids = null
) {
    if (!is_string($path) || $path === '' || is_link($path)) return false;
    $directory = dirname($path);
    if (!is_dir($directory) || is_link($directory)) return false;
    $existed = file_exists($path) || is_link($path);
    $before = $existed ? @lstat($path) : null;
    $expectedMode = ((int)$mode) & 0777;
    $ownerUids = is_array($allowedOwnerUids)
        ? array_values(array_unique(array_map('intval', $allowedOwnerUids)))
        : null;
    $strictSharedLock = $expectedGid !== null || $ownerUids !== null;
    if ($existed && (!is_array($before)
        || ((((int)$before['mode']) & 0170000) !== 0100000)
        || (int)$before['nlink'] !== 1
        || ($strictSharedLock && (int)$before['size'] > 65536)
        || ((((int)$before['mode']) & 0777) !== $expectedMode)
        || ($expectedGid !== null && (int)$before['gid'] !== (int)$expectedGid)
        || ($ownerUids !== null && !in_array((int)$before['uid'], $ownerUids, true)))) {
        return false;
    }
    // Der strikte Modus erzeugt 0660 bereits beim O_EXCL-Create. PHP bietet
    // kein portables fchmod für Streams; ein chmod über den Namen wäre nach
    // dem Öffnen rename-racebar und dürfte einen Fremdersatz normalisieren.
    $oldUmask = umask($strictSharedLock ? 0006 : 0002);
    $handle = @fopen(
        $path,
        $strictSharedLock ? ($existed ? 'r+b' : 'x+b') : 'c+b'
    );
    umask($oldUmask);
    if ($handle === false) return false;
    $meta = @fstat($handle);
    if (!is_array($meta)
        || (((int)$meta['mode']) & 0170000) !== 0100000
        || (int)$meta['nlink'] !== 1) {
        @fclose($handle);
        return false;
    }
    if ($existed) {
        if ((int)$meta['dev'] !== (int)$before['dev']
            || (int)$meta['ino'] !== (int)$before['ino']) {
            @fclose($handle);
            return false;
        }
        if ($expectedGid === null && ((((int)$meta['mode']) & 0777) !== $expectedMode) && !@chmod($path, (int)$mode)) {
            @fclose($handle);
            return false;
        }
    } else {
        if (($expectedGid !== null && (int)$meta['gid'] !== (int)$expectedGid)
            || ($ownerUids !== null && !in_array((int)$meta['uid'], $ownerUids, true))
            || (!$strictSharedLock && !@chmod($path, (int)$mode))) {
            @fclose($handle);
            return false;
        }
    }
    $meta = @fstat($handle);
    if (!is_array($meta)
        || ((((int)$meta['mode']) & 0777) !== $expectedMode)
        || ($strictSharedLock && (int)$meta['size'] > 65536)
        || ($expectedGid !== null && (int)$meta['gid'] !== (int)$expectedGid)
        || ($ownerUids !== null && !in_array((int)$meta['uid'], $ownerUids, true))) {
        @fclose($handle);
        return false;
    }
    if ($strictSharedLock) {
        $named = @lstat($path);
        if (!is_array($named)
            || ((((int)$named['mode']) & 0170000) !== 0100000)
            || (int)$named['nlink'] !== 1
            || (int)$named['dev'] !== (int)$meta['dev']
            || (int)$named['ino'] !== (int)$meta['ino']
            || (int)$named['size'] !== (int)$meta['size']
            || (int)$named['size'] > 65536
            || ((((int)$named['mode']) & 0777) !== $expectedMode)
            || ($expectedGid !== null && (int)$named['gid'] !== (int)$expectedGid)
            || ($ownerUids !== null && !in_array((int)$named['uid'], $ownerUids, true))) {
            @fclose($handle);
            return false;
        }
    }
    $deadline = microtime(true) + max(0.1, min(10.0, (float)$timeout));
    do {
        if (@flock($handle, LOCK_EX | LOCK_NB)) {
            if ($strictSharedLock) {
                $lockedMeta = @fstat($handle);
                clearstatcache(true, $path);
                $lockedNamed = @lstat($path);
                if (!is_array($lockedMeta)
                    || !is_array($lockedNamed)
                    || ((((int)$lockedMeta['mode']) & 0170000) !== 0100000)
                    || ((((int)$lockedNamed['mode']) & 0170000) !== 0100000)
                    || (int)$lockedMeta['nlink'] !== 1
                    || (int)$lockedNamed['nlink'] !== 1
                    || (int)$lockedMeta['dev'] !== (int)$lockedNamed['dev']
                    || (int)$lockedMeta['ino'] !== (int)$lockedNamed['ino']
                    || (int)$lockedMeta['size'] !== (int)$lockedNamed['size']
                    || (int)$lockedMeta['size'] > 65536
                    || ((((int)$lockedMeta['mode']) & 0777) !== $expectedMode)
                    || ((((int)$lockedNamed['mode']) & 0777) !== $expectedMode)
                    || ($expectedGid !== null
                        && ((int)$lockedMeta['gid'] !== (int)$expectedGid
                            || (int)$lockedNamed['gid'] !== (int)$expectedGid))
                    || ($ownerUids !== null
                        && (!in_array((int)$lockedMeta['uid'], $ownerUids, true)
                            || !in_array((int)$lockedNamed['uid'], $ownerUids, true)))) {
                    @flock($handle, LOCK_UN);
                    @fclose($handle);
                    return false;
                }
            }
            return $handle;
        }
        usleep(20000);
    } while (microtime(true) < $deadline);
    @fclose($handle);
    return false;
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

function e3dcWbTxRuntimeWb2CandidateContract($path, $nowTs = null) {
    $raw = e3dcReadRegularFileBound($path, 4194304);
    $payload = !is_string($raw) || strlen($raw) < 2
        ? null
        : json_decode($raw, true);
    $now = is_numeric($nowTs) ? (float)$nowTs : (float)time();
    if (!e3dcWallbox2RuntimeEvidence($payload, $now, 60.0)) return null;

    $details = [];
    foreach (($payload['wb_details'] ?? []) as $detail) {
        if (!is_array($detail)) continue;
        $id = (int)($detail['id'] ?? 0);
        if (($id === 1 || $id === 2) && !isset($details[$id])) {
            $details[$id] = $detail;
        }
    }
    $discovery = $details[2]['chargepoint_discovery_contract'];
    $wb1Output = $details[1]['physical_output_contract'];
    $wb2Output = $details[2]['physical_output_contract'];
    return [
        'schema_version' => 'wallbox_candidate_runtime_wb2_v1',
        'valid' => true,
        'manager_ts' => (float)$payload['ts'],
        'source' => (string)$discovery['source'],
        'detected_at' => (float)$discovery['detected_at'],
        'status_confirmed' => true,
        'status_confirmed_ts' => (float)$discovery['status_confirmed_ts'],
        'controller_identity' => (string)$wb2Output['controller_identity'],
        'endpoint_kind' => (string)$wb2Output['endpoint_kind'],
        'cp_id' => (int)$discovery['cp_id'],
        'peer_cp_id' => (int)$discovery['peer_cp_id'],
        'physical_output_identity' => (string)$wb2Output['identity'],
        'peer_physical_output_identity' => (string)$wb1Output['identity'],
        'physical_output_allowed' => true,
    ];
}

function e3dcWbTxManualPlanRequired(array $flat, $wbId, $runtimeWb2 = null) {
    if ((int)$wbId === 2) {
        if (isWallbox2ExplicitlyDisabledConfig($flat)) return false;
        if (!hasWallbox2ExplicitConfig($flat) && !is_array($runtimeWb2)) {
            return false;
        }
    }
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

function e3dcWbTxModeRequestBytes(array $snapshot, $wbId, $newMode, $oldMode, $kind, array $meta = []) {
    $data = [];
    if (!empty($snapshot['exists'])) {
        $decoded = json_decode((string)$snapshot['bytes'], true);
        if (!is_array($decoded)) throw new RuntimeException('Vorhandene Wallbox-Anforderungsdatei ist ungültig.');
        $data = $decoded;
    }
    $key = (string)max(1, min(2, (int)$wbId));
    if ($kind === 'default') {
        $requestId = strtolower(trim((string)($meta['request_id'] ?? '')));
        $candidateSha = strtolower(trim((string)($meta['candidate_config_sha256'] ?? '')));
        $previousMode = trim((string)$oldMode);
        if (!preg_match('/^[a-f0-9]{32}$/', $requestId)
            || !preg_match('/^[a-f0-9]{64}$/', $candidateSha)
            || (string)$newMode !== '0'
            || !in_array($previousMode, ['0', '2', '3', '4', '5', '12'], true)) {
            throw new InvalidArgumentException('Mode-0-Übergabe ist nicht vollständig gebunden.');
        }
        $data[$key] = [
            'schema' => 'wallbox_mode0_default_release_request_v2',
            'request_id' => $requestId,
            'candidate_config_sha256' => $candidateSha,
            'wb' => (int)$key,
            'target_mode' => '0',
            'previous_mode' => $previousMode,
            'ts' => time(),
            'source' => 'Wallbox.php',
            'reason' => 'mode0_user_switch',
        ];
    } elseif ($kind === 'user') {
        $data[$key] = [
            'ts' => time(), 'source' => 'Wallbox.php', 'reason' => 'mode2_user_switch_pv',
            'target_mode' => '2', 'previous_mode' => (string)$oldMode,
        ];
    } elseif ($kind === 'mode5_start') {
        if (!empty($data)) {
            throw new RuntimeException('Eine Sofortlade-Anforderung wartet noch auf ihr exaktes ACK.');
        }
        // Die persistente Mode-5-Fläche ist absichtlich ein einzelner Slot.
        // Der Manager bestätigt sie ausschließlich per exaktem request_id und
        // löscht danach die PHP-eigene Datei, statt sie unter fremder UID neu
        // zu schreiben. Auch dieselbe Wallbox darf einen noch nicht quittierten
        // Slot nicht ersetzen; Crash-Replay besitzt Vorrang vor einem Re-arm.
        $data = [];
        $requestId = strtolower(trim((string)($meta['request_id'] ?? '')));
        $candidateSha = strtolower(trim((string)($meta['candidate_config_sha256'] ?? '')));
        $plugSessionId = trim((string)($meta['expected_plug_session_id'] ?? ''));
        $bootId = trim((string)($meta['expected_boot_id'] ?? ''));
        $priceLimit = $meta['price_limit_ct'] ?? null;
        $intentTs = $meta['intent_ts'] ?? null;
        $latchGeneration = $meta['expected_latch_generation'] ?? null;
        $latchState = is_array($latchGeneration)
            ? (string)($latchGeneration['state'] ?? '')
            : '';
        $latchShapeValid = is_array($latchGeneration)
            && ($latchGeneration['schema'] ?? '') === 'wallbox_mode5_latch_generation_v1'
            && (int)($latchGeneration['wb'] ?? 0) === (int)$key
            && in_array($latchState, ['absent', 'present'], true);
        if ($latchShapeValid && $latchState === 'absent') {
            $latchShapeValid = array_keys($latchGeneration) === [
                'schema', 'state', 'wb',
            ];
        } elseif ($latchShapeValid) {
            $latchReason = trim((string)($latchGeneration['reason'] ?? ''));
            $latchSession = trim((string)($latchGeneration['plug_session_id'] ?? ''));
            $latchToken = (string)($latchGeneration['release_token'] ?? '');
            $latchTs = $latchGeneration['latched_ts'] ?? null;
            $latchShapeValid = $latchReason !== '' && strlen($latchReason) <= 128
                && $latchSession !== '' && strlen($latchSession) <= 256
                && $latchToken !== '' && strlen($latchToken) <= 4096
                && is_numeric($latchTs)
                && is_finite((float)$latchTs)
                && (float)$latchTs > 0.0;
        }
        if (!preg_match('/^[a-f0-9]{32}$/', $requestId)
            || !preg_match('/^[a-f0-9]{64}$/', $candidateSha)
            || $plugSessionId === ''
            || strlen($plugSessionId) > 256
            || $bootId === ''
            || strlen($bootId) > 128
            || !is_numeric($priceLimit)
            || !is_finite((float)$priceLimit)
            || (float)$priceLimit < 0.0
            || (float)$priceLimit > 200.0
            || !is_numeric($intentTs)
            || !is_finite((float)$intentTs)
            || (float)$intentTs <= 0.0
            || (float)$intentTs > microtime(true) + 5.0
            || !$latchShapeValid) {
            throw new InvalidArgumentException('Sofortlade-Anforderung ist nicht vollständig gebunden.');
        }
        $data[$key] = [
            'schema' => 'wallbox_mode5_user_start_request_v1',
            'request_id' => $requestId,
            'ts' => (float)$intentTs,
            'source' => 'Wallbox.php',
            'wb' => (int)$key,
            'target_mode' => '5',
            'previous_mode' => (string)$oldMode,
            'reason' => 'mode5_user_start',
            'charge_intent' => 'instant',
            'energy_mode' => 'grid_price',
            'expected_plug_session_id' => $plugSessionId,
            'expected_boot_id' => $bootId,
            'expected_latch_generation' => $latchGeneration,
            'candidate_config_sha256' => $candidateSha,
            'price_limit_ct' => (float)$priceLimit,
        ];
    } else {
        throw new InvalidArgumentException('Unbekannte Wallbox-Modusanforderung.');
    }
    $json = json_encode($data, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    if ($json === false) throw new RuntimeException('Wallbox-Anforderung konnte nicht kodiert werden.');
    return $json . "\n";
}

function e3dcWbTxMode5SessionBinding($path, $wbId) {
    $snapshot = e3dcWbTxSnapshot($path, 4194304);
    if (empty($snapshot['exists'])) {
        throw new RuntimeException('Persistenter openWB-Pro-Sitzungsstand fehlt.');
    }
    $state = json_decode((string)$snapshot['bytes'], true);
    $key = (string)max(1, min(2, (int)$wbId));
    $entry = is_array($state) && isset($state['wallboxes'][$key]) && is_array($state['wallboxes'][$key])
        ? $state['wallboxes'][$key]
        : [];
    $bootId = trim((string)($state['boot_id'] ?? ''));
    $plugSessionId = trim((string)($entry['openwb_pro_plug_session_id'] ?? ''));
    $receipt = $entry['mode5_user_start_request_receipt'] ?? null;
    if ($receipt !== null) {
        $receiptState = is_array($receipt)
            ? trim((string)($receipt['state'] ?? ''))
            : '';
        $receiptId = is_array($receipt)
            ? strtolower(trim((string)($receipt['request_id'] ?? '')))
            : '';
        $receiptSession = is_array($receipt)
            ? trim((string)($receipt['plug_session_id'] ?? ''))
            : '';
        $receiptBoot = is_array($receipt)
            ? trim((string)($receipt['expected_boot_id'] ?? ''))
            : '';
        if (!is_array($receipt)
            || ($receipt['schema'] ?? '') !== 'wallbox_mode5_user_start_receipt_v1'
            || !in_array($receiptState, ['prepared', 'committed', 'acked'], true)
            || !preg_match('/^[a-f0-9]{32}$/', $receiptId)
            || $receiptSession !== $plugSessionId
            || $receiptBoot !== $bootId) {
            throw new RuntimeException('Persistentes Sofortlade-Receipt ist nicht vertrauenswürdig.');
        }
        if ($receiptState === 'prepared') {
            throw new RuntimeException('Eine vorbereitete Sofortlade-Freigabe muss zuerst fortgesetzt werden.');
        }
    }
    if (($state['schema'] ?? '') !== 'wallbox_phase_state_v3'
        || $bootId === ''
        || strlen($bootId) > 128
        || $plugSessionId === ''
        || strlen($plugSessionId) > 256) {
        throw new RuntimeException('openWB-Pro-Sitzung ist nicht eindeutig gebunden.');
    }
    return [
        'expected_plug_session_id' => $plugSessionId,
        'expected_boot_id' => $bootId,
    ];
}

function e3dcWbTxMode5LatchGeneration($path, $wbId) {
    $key = (string)max(1, min(2, (int)$wbId));
    $absent = [
        'schema' => 'wallbox_mode5_latch_generation_v1',
        'state' => 'absent',
        'wb' => (int)$key,
    ];
    $snapshot = e3dcWbTxSnapshot($path, 4194304);
    if (empty($snapshot['exists'])) return $absent;
    $state = json_decode((string)$snapshot['bytes'], true);
    if (!is_array($state)
        || ($state['schema'] ?? '') !== 'wallbox_abort_state_v2') {
        throw new RuntimeException('Persistenter Wallbox-Latchzustand ist nicht vertrauenswürdig.');
    }
    $entry = $state[$key] ?? null;
    if ($entry === null) return $absent;
    if (!is_array($entry)) {
        throw new RuntimeException('Persistente Wallbox-Latchgeneration ist nicht eindeutig.');
    }
    if (($entry['bev_full_blocked'] ?? false) !== true) return $absent;
    $reason = trim((string)($entry['bev_full_block_reason'] ?? ''));
    $plugSessionId = trim((string)($entry['plug_session_id'] ?? ''));
    $releaseToken = (string)($entry['charge_end_release_token'] ?? '');
    $latchedTs = $entry['_charge_end_latched_ts'] ?? null;
    if ($reason === '' || strlen($reason) > 128
        || $plugSessionId === '' || strlen($plugSessionId) > 256
        || $releaseToken === '' || strlen($releaseToken) > 4096
        || !is_numeric($latchedTs)
        || !is_finite((float)$latchedTs)
        || (float)$latchedTs <= 0.0) {
        throw new RuntimeException('Persistente Wallbox-Latchgeneration ist unvollständig.');
    }
    return [
        'schema' => 'wallbox_mode5_latch_generation_v1',
        'state' => 'present',
        'wb' => (int)$key,
        'reason' => $reason,
        'plug_session_id' => $plugSessionId,
        'latched_ts' => (float)$latchedTs,
        'release_token' => $releaseToken,
    ];
}

function e3dcWallboxPlanTransaction(array $updates, array $options = []) {
    // Die Nutzerannahme muss vor jeder Sperre und vor dem Planner feststehen.
    // Ihr Zeitpunkt wird erst nach dem initialen Sitzungs-/Latch-Snapshot
    // gesetzt: Nur diese bereits gebundene Generation kann der Klick lösen;
    // jede danach entstehende Generation fällt beim Commit-Rebind durch.
    $modeTransition = isset($options['mode_transition']) && is_array($options['mode_transition'])
        ? $options['mode_transition'] : null;
    $mode5IntentRequested = false;
    $mode5IntentTs = null;
    $mode5WbId = 0;
    if (is_array($modeTransition)
        && (string)($modeTransition['new_mode'] ?? '') === '5'
        && (string)($modeTransition['charge_intent'] ?? '') === 'instant'
        && (string)($modeTransition['energy_mode'] ?? '') === 'grid_price') {
        $mode5IntentRequested = true;
        $mode5WbId = max(1, min(2, (int)($modeTransition['wb_id'] ?? 1)));
    }
    $txId = '';
    $jobDir = '';
    $lock = null;
    $requestLocks = [];
    $mode5SessionBinding = [];
    $mode5LatchGeneration = [];
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
        if ($mode5IntentRequested) {
            $mode5SessionBinding = e3dcWbTxMode5SessionBinding(
                dirname($context['config_path']) . '/wallbox_phase_transition_state.json',
                $mode5WbId
            );
            $mode5LatchGeneration = e3dcWbTxMode5LatchGeneration(
                $context['ramdisk_dir'] . '/wallbox_abort_state.json',
                $mode5WbId
            );
            $mode5IntentTs = microtime(true);
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
        // Der Konfigurationscache ist ein abgeleiteter tmpfs-Spiegel und wird
        // von jedem regulären Lesezugriff bei Bedarf neu erzeugt. Er ist weder
        // kanonischer Transaktionszustand noch eine zulässige Parallelitäts-
        // autorität; andernfalls kann ein gleichzeitiger Dashboardabruf jeden
        // legitimen Wallbox-Save als concurrent_change verwerfen.
        $targets = [$context['config_path'], ...array_values($planTargets)];
        if ($savedCarsRequested) $targets[] = $savedCarsPath;

        if ($abortAction !== 'preserve') $targets[] = $abortPath;
        if ($emergencyAction !== 'preserve') $targets[] = $emergencyPath;

        $manualSoc = isset($options['manual_soc']) && is_array($options['manual_soc']) ? $options['manual_soc'] : [];
        foreach ($manualSoc as $wbId => $payload) {
            $wbId = max(1, min(2, (int)$wbId));
            $targets[] = $ramdisk . '/manual_soc_wb' . $wbId . '.json';
        }

        $requestTarget = null;
        $requestKind = null;
        if ($modeTransition) {
            // Jeder Moduswechsel teilt sich dieselbe kurze Sperre mit dem
            // openWB-Pro-Handoff. So kann ein Wechsel zurück in einen aktiven
            // Modus nicht zwischen dessen Config-Bindung und genau einem
            // sicheren 0-A-/Heartbeat-Ausgang committen.
            $defaultReleaseLockPath = $ramdisk . '/wallbox_default_release_request.json.lock';
            $defaultReleaseLock = e3dcWbTxAcquireSharedRequestLock(
                $defaultReleaseLockPath,
                (float)($options['lock_timeout'] ?? 2.0),
                0664
            );
            if ($defaultReleaseLock === false) {
                throw new RuntimeException('Gemeinsame Wallbox-Übergabesperre ist belegt oder unsicher.');
            }
            $requestLocks[$defaultReleaseLockPath] = $defaultReleaseLock;

            $newMode = (string)($modeTransition['new_mode'] ?? '');
            $oldMode = (string)($modeTransition['old_mode'] ?? '');
            if ($newMode === '0' && $oldMode !== '0') {
                $requestKind = 'default';
                $requestTarget = $ramdisk . '/wallbox_default_release_request.json';
            } elseif ($newMode === '2' && $oldMode !== '2') {
                $requestKind = 'user';
                $requestTarget = $ramdisk . '/wallbox_user_mode_request.json';
            } elseif (
                $newMode === '5'
                && (string)($modeTransition['charge_intent'] ?? '') === 'instant'
                && (string)($modeTransition['energy_mode'] ?? '') === 'grid_price'
            ) {
                $requestKind = 'mode5_start';
                $requestTarget = dirname($context['config_path']) . '/wallbox_mode5_user_start_request.json';
                $priceLimit = $modeTransition['price_limit_ct'] ?? null;
                if (!is_numeric($priceLimit)
                    || !is_finite((float)$priceLimit)
                    || (float)$priceLimit < 0.0
                    || (float)$priceLimit > 200.0) {
                    throw new InvalidArgumentException('Preislimit für Sofortladen ist ungültig.');
                }
            }
            if ($requestTarget !== null) {
                $mode5Surface = null;
                if ($requestKind === 'mode5_start') {
                    $mode5Surface = e3dcWbTxMode5SurfaceContract($requestTarget);
                    if (!is_array($mode5Surface)) {
                        throw new RuntimeException('Persistente Sofortlade-Anforderungsfläche ist nicht vertrauenswürdig.');
                    }
                }
                if (in_array($requestKind, ['default', 'user', 'mode5_start'], true)) {
                    $mode5LockOwners = null;
                    if ($requestKind === 'mode5_start') {
                        $mode5LockOwners = e3dcWbTxMode5AllowedLockUids();
                    }
                    $requestLockPath = $requestTarget . '.lock';
                    if (!isset($requestLocks[$requestLockPath])) {
                        $requestLock = e3dcWbTxAcquireSharedRequestLock(
                            $requestLockPath,
                            (float)($options['lock_timeout'] ?? 2.0),
                            $requestKind === 'mode5_start' ? 0660 : 0664,
                            $requestKind === 'mode5_start' ? (int)$mode5Surface['gid'] : null,
                            $mode5LockOwners
                        );
                        if ($requestLock === false) {
                            throw new RuntimeException('Gemeinsame Wallbox-Anforderungssperre ist belegt oder unsicher.');
                        }
                        $requestLocks[$requestLockPath] = $requestLock;
                    }
                }
                $targets[] = $requestTarget;
            }
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
        $runtimeWb2 = null;
        if (!isWallbox2ExplicitlyDisabledConfig($flatCandidate)
            && !hasWallbox2ExplicitConfig($flatCandidate)) {
            $runtimeWb2 = e3dcWbTxRuntimeWb2CandidateContract(
                $ramdisk . '/wallbox_native.json'
            );
        }
        $required = [];
        foreach ([1, 2] as $wbId) if (e3dcWbTxManualPlanRequired($flatCandidate, $wbId, $runtimeWb2)) $required[] = $wbId;
        $candidateRequest = [
            'schema' => E3DC_WB_TX_SCHEMA,
            'operation' => $operation,
            'require_plan' => $required,
        ];
        if (is_array($runtimeWb2)) {
            $candidateRequest['runtime_wb2'] = $runtimeWb2;
        }
        if (!e3dcWbTxPrivateJson($jobDir . '/candidate_request.json', $candidateRequest)) throw new RuntimeException('Planner-Auftrag konnte nicht geschrieben werden.');
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
        if ($requestKind === 'mode5_start') {
            $committedCandidate = json_decode((string)$candidateBytes, true);
            if (!is_array($committedCandidate)) {
                throw new RuntimeException('Sofortlade-Konfigurationskandidat ist nicht lesbar.');
            }
            $committedFlat = e3dcWbTxFlattenConfig($committedCandidate);
            $wbId = max(1, min(2, (int)($modeTransition['wb_id'] ?? 1)));
            $committedMode = (string)($committedFlat['wb' . $wbId . '_mode'] ?? '');
            $committedPrice = $committedFlat['dvcarlimit'] ?? null;
            $requestedPrice = $modeTransition['price_limit_ct'] ?? null;
            if ($committedMode !== '5'
                || !is_numeric($committedPrice)
                || !is_numeric($requestedPrice)
                || !is_finite((float)$committedPrice)
                || !is_finite((float)$requestedPrice)
                || abs((float)$committedPrice - (float)$requestedPrice) > 0.000001) {
                throw new RuntimeException('Sofortlade-Anforderung passt nicht zum versiegelten Konfigurationskandidaten.');
            }
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
        if ($requestKind === 'mode5_start') {
            $commitBinding = e3dcWbTxMode5SessionBinding(
                dirname($context['config_path']) . '/wallbox_phase_transition_state.json',
                $mode5WbId
            );
            if (!hash_equals(
                    (string)($mode5SessionBinding['expected_plug_session_id'] ?? ''),
                    (string)($commitBinding['expected_plug_session_id'] ?? '')
                )
                || !hash_equals(
                    (string)($mode5SessionBinding['expected_boot_id'] ?? ''),
                    (string)($commitBinding['expected_boot_id'] ?? '')
                )) {
                return e3dcWbTxResult(
                    false,
                    'concurrent_change',
                    'Die openWB-Pro-Steck- oder Boot-Sitzung wechselte während der Planung; nichts wurde übernommen.',
                    ['planner' => $planner, 'transaction_id' => $txId]
                );
            }
            $commitLatchGeneration = e3dcWbTxMode5LatchGeneration(
                $context['ramdisk_dir'] . '/wallbox_abort_state.json',
                $mode5WbId
            );
            $initialLatchBytes = json_encode(
                $mode5LatchGeneration,
                JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE
            );
            $commitLatchBytes = json_encode(
                $commitLatchGeneration,
                JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE
            );
            if (!is_string($initialLatchBytes)
                || !is_string($commitLatchBytes)
                || !hash_equals($initialLatchBytes, $commitLatchBytes)) {
                return e3dcWbTxResult(
                    false,
                    'concurrent_change',
                    'Die Wallbox-Latchgeneration wechselte während der Planung; nichts wurde übernommen.',
                    ['planner' => $planner, 'transaction_id' => $txId]
                );
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
                    $requestKind,
                    [
                        'request_id' => $txId,
                        'candidate_config_sha256' => hash('sha256', $candidateBytes),
                        'price_limit_ct' => $modeTransition['price_limit_ct'] ?? null,
                        'expected_plug_session_id' => $mode5SessionBinding['expected_plug_session_id'] ?? '',
                        'expected_boot_id' => $mode5SessionBinding['expected_boot_id'] ?? '',
                        'intent_ts' => $mode5IntentTs,
                        'expected_latch_generation' => $mode5LatchGeneration,
                    ]
                ),
                $requestKind === 'mode5_start'
                    ? 0660
                    : (!empty($snapshots[$requestTarget]['exists']) ? $snapshots[$requestTarget]['mode'] : 0664),
                $requestKind === 'mode5_start',
            ];
        }
        // Entfernen/Erzeugen des Notfallmarkers ist bewusst der letzte öffentliche Schreibzugriff.
        // Ein früher fehlgeschlagener Auftrag kann deshalb nie eine unverriegelte Wallbox freigeben.
        if ($emergencyAction === 'remove') $desired[] = [$emergencyPath, null, 0600];
        if ($emergencyAction === 'create') $desired[] = [$emergencyPath, gmdate('c') . "\n", !empty($snapshots[$emergencyPath]['exists']) ? $snapshots[$emergencyPath]['mode'] : 0644];

        foreach ($desired as $index => $entry) {
            $path = $entry[0];
            $bytes = $entry[1];
            $mode = $entry[2];
            $strictMode5Surface = !empty($entry[3]);
            if ($context['test'] && isset($options['fail_commit_at']) && (string)$options['fail_commit_at'] === basename($path)) {
                throw new RuntimeException('Injected commit failure');
            }
            $failureStage = '';
            if ($context['test']
                && isset($options['fail_durable_at'], $options['fail_durable_stage'])
                && (string)$options['fail_durable_at'] === basename($path)) {
                $failureStage = (string)$options['fail_durable_stage'];
            }
            $publishedMutation = false;
            $applied = e3dcWbTxApply(
                $path,
                $bytes,
                $mode,
                $failureStage,
                $strictMode5Surface,
                $publishedMutation
            );
            // Auch ein nach rename fehlschlagendes Parent-fsync hat das Ziel
            // bereits verändert und muss daher zwingend in die exakte
            // Snapshot-Rückabwicklung aufgenommen werden. Ein vor rename
            // verworfenes fremdes Ziel wird dagegen niemals angefasst.
            if ($publishedMutation) $mutated[] = $path;
            if (!$applied) {
                throw new RuntimeException('Commit fehlgeschlagen: ' . basename($path));
            }
        }
        // Nach dem kanonischen Commit wird die reine Cacheprojektion nur
        // best-effort verworfen. Ein paralleler Leser darf sie davor oder
        // danach neu aufbauen; loadE3dcConfig() bindet sie an die aktuelle
        // Konfigurationsgeneration und liest die Quelle ohnehin erneut.
        if (is_file($cachePath) && !is_link($cachePath)) {
            @unlink($cachePath);
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
        foreach (array_reverse($requestLocks, true) as $requestLock) {
            if (!is_resource($requestLock)) continue;
            @flock($requestLock, LOCK_UN);
            @fclose($requestLock);
        }
        if (is_resource($lock)) {
            @flock($lock, LOCK_UN);
            @fclose($lock);
        }
    }
}
