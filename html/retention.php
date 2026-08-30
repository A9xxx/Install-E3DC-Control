<?php
/**
 * Sichere Aufbewahrungsregeln für lokale Konfigurations- und Tagesarchive.
 *
 * Unbekannte Namen, symbolische Links und nicht stabil gebundene Dateien
 * werden grundsätzlich nicht gelöscht.
 */

function e3dcRetentionRegularFileSnapshot($path) {
    clearstatcache(true, (string)$path);
    $stat = @lstat((string)$path);
    if (!is_array($stat)
        || ((((int)($stat['mode'] ?? 0)) & 0170000) !== 0100000)
        || (int)($stat['nlink'] ?? 0) !== 1
        || is_link((string)$path)) {
        return null;
    }
    return [
        'dev' => (int)($stat['dev'] ?? -1),
        'ino' => (int)($stat['ino'] ?? -1),
        'mode' => (int)($stat['mode'] ?? 0),
        'nlink' => (int)($stat['nlink'] ?? 0),
        'size' => (int)($stat['size'] ?? -1),
        'mtime' => (int)($stat['mtime'] ?? -1),
    ];
}

function e3dcRetentionSameFileGeneration($path, $bound) {
    if (!is_array($bound)) return false;
    $current = e3dcRetentionRegularFileSnapshot($path);
    if (!is_array($current)) return false;
    foreach (['dev', 'ino', 'mode', 'nlink', 'size', 'mtime'] as $key) {
        if ((int)$current[$key] !== (int)($bound[$key] ?? -2)) return false;
    }
    return true;
}

function e3dcRetentionOpenedRegularFileSnapshot($handle) {
    if (!is_resource($handle)) return null;
    $stat = @fstat($handle);
    if (!is_array($stat)
        || ((((int)($stat['mode'] ?? 0)) & 0170000) !== 0100000)
        || (int)($stat['nlink'] ?? 0) !== 1) {
        return null;
    }
    return [
        'dev' => (int)($stat['dev'] ?? -1),
        'ino' => (int)($stat['ino'] ?? -1),
        'mode' => (int)($stat['mode'] ?? 0),
        'nlink' => (int)($stat['nlink'] ?? 0),
        'size' => (int)($stat['size'] ?? -1),
        'mtime' => (int)($stat['mtime'] ?? -1),
    ];
}

function e3dcRetentionOpenedMatchesNamedFile($handle, $path, $expected = null) {
    $opened = e3dcRetentionOpenedRegularFileSnapshot($handle);
    $named = e3dcRetentionRegularFileSnapshot($path);
    if (!is_array($opened) || !is_array($named)) return false;
    foreach (['dev', 'ino', 'mode', 'nlink', 'size', 'mtime'] as $key) {
        if ((int)$opened[$key] !== (int)$named[$key]) return false;
        if (is_array($expected)
            && (int)$opened[$key] !== (int)($expected[$key] ?? -2)) {
            return false;
        }
    }
    return true;
}

function e3dcRetentionProcessFileDescriptorSnapshots() {
    if (PHP_OS_FAMILY !== 'Linux' || !is_dir('/proc/self/fd')) return null;
    $paths = @glob('/proc/self/fd/[0-9]*');
    if (!is_array($paths)) return null;

    $snapshots = [];
    foreach ($paths as $path) {
        clearstatcache(true, $path);
        $stat = @stat($path);
        if (!is_array($stat)
            || ((((int)($stat['mode'] ?? 0)) & 0170000) !== 0100000)) {
            continue;
        }
        $snapshots[(string)basename($path)] = [
            'dev' => (int)($stat['dev'] ?? -1),
            'ino' => (int)($stat['ino'] ?? -1),
            'mode' => (int)($stat['mode'] ?? 0),
            'nlink' => (int)($stat['nlink'] ?? 0),
            'size' => (int)($stat['size'] ?? -1),
            'mtime' => (int)($stat['mtime'] ?? -1),
        ];
    }
    return $snapshots;
}

function e3dcRetentionSnapshotsMatch($left, $right) {
    if (!is_array($left) || !is_array($right)) return false;
    foreach (['dev', 'ino', 'mode', 'nlink', 'size', 'mtime'] as $key) {
        if ((int)($left[$key] ?? -1) !== (int)($right[$key] ?? -2)) return false;
    }
    return true;
}

function e3dcRetentionNewBoundDescriptors($before, $after, $bound) {
    if (!is_array($before) || !is_array($after) || !is_array($bound)) return null;
    $descriptors = [];
    foreach ($after as $descriptor => $snapshot) {
        if (!e3dcRetentionSnapshotsMatch($snapshot, $bound)) continue;
        if (!array_key_exists($descriptor, $before)
            || !e3dcRetentionSnapshotsMatch($before[$descriptor], $snapshot)) {
            $descriptors[$descriptor] = $snapshot;
        }
    }
    return $descriptors;
}

function e3dcRetentionHasStillBoundDescriptor($opened, $current, $bound) {
    if (!is_array($opened) || !is_array($current) || !is_array($bound)) return false;
    foreach ($opened as $descriptor => $snapshot) {
        if (isset($current[$descriptor])
            && e3dcRetentionSnapshotsMatch($snapshot, $bound)
            && e3dcRetentionSnapshotsMatch($current[$descriptor], $bound)) {
            return true;
        }
    }
    return false;
}

function e3dcRetentionRestoreQuarantinedFile($quarantine, $path) {
    clearstatcache(true, $path);
    if (file_exists($path) || is_link($path)) return false;
    // link() stellt nur wieder her, wenn der öffentliche Name weiterhin frei
    // ist. Eine zwischenzeitlich neu angelegte Datei wird nie überschrieben.
    if (!@link($quarantine, $path)) return false;
    return @unlink($quarantine);
}

function e3dcRetentionDeleteBoundFile($path, $bound, $options = []) {
    $path = (string)$path;
    if (!e3dcRetentionSameFileGeneration($path, $bound)) {
        return ['success' => false, 'status' => 'generation_drift'];
    }

    try {
        $random = bin2hex(random_bytes(16));
    } catch (Throwable $error) {
        return ['success' => false, 'status' => 'quarantine_name_failed'];
    }
    $directory = dirname($path);
    $quarantineDirectory = $directory . '/.e3dc-retention-delete-' . $random;
    // Der Originalname bleibt innerhalb der privaten Quarantäne erhalten,
    // damit auch ein Prozessabbruch vor dem Unlink keinen namenlosen Rest lässt.
    $quarantine = $quarantineDirectory . '/' . basename($path);
    if (!@mkdir($quarantineDirectory, 0700) || !@chmod($quarantineDirectory, 0700)) {
        @rmdir($quarantineDirectory);
        return ['success' => false, 'status' => 'quarantine_create_failed'];
    }

    // Ab hier wird der öffentliche Name nie unlinkt. rename() löst genau die
    // aktuell benannte Generation atomar aus dem öffentlichen Namensraum. War
    // sie nach dem letzten Check ausgetauscht, wird sie nicht gelöscht, sondern
    // per No-Clobber-Hardlink zurückgelegt oder in der privaten Quarantäne
    // erhalten.
    if (!e3dcRetentionSameFileGeneration($path, $bound)) {
        @rmdir($quarantineDirectory);
        return ['success' => false, 'status' => 'generation_drift'];
    }
    if (!@rename($path, $quarantine)) {
        @rmdir($quarantineDirectory);
        return ['success' => false, 'status' => 'quarantine_move_failed'];
    }
    if (!e3dcRetentionSameFileGeneration($quarantine, $bound)) {
        $restored = e3dcRetentionRestoreQuarantinedFile($quarantine, $path);
        if ($restored) @rmdir($quarantineDirectory);
        return [
            'success' => false,
            'status' => $restored ? 'generation_drift' : 'generation_drift_quarantined',
        ];
    }

    $handle = @fopen($quarantine, 'rb');
    if (!is_resource($handle)) {
        $restored = e3dcRetentionRestoreQuarantinedFile($quarantine, $path);
        if ($restored) @rmdir($quarantineDirectory);
        return [
            'success' => false,
            'status' => $restored ? 'quarantine_open_failed' : 'quarantine_open_failed_preserved',
        ];
    }

    $locked = false;
    $failure = null;
    try {
        $locked = @flock($handle, LOCK_EX);
        if (!$locked
            || !e3dcRetentionOpenedMatchesNamedFile($handle, $quarantine, $bound)) {
            $failure = 'quarantine_generation_drift';
        } elseif (!@unlink($quarantine)) {
            $failure = 'quarantine_unlink_failed';
        }
    } finally {
        if ($locked) @flock($handle, LOCK_UN);
        @fclose($handle);
    }

    if ($failure !== null) {
        $restored = e3dcRetentionRestoreQuarantinedFile($quarantine, $path);
        if ($restored) @rmdir($quarantineDirectory);
        return [
            'success' => false,
            'status' => $restored ? $failure : $failure . '_preserved',
        ];
    }

    if (!@rmdir($quarantineDirectory)) {
        return ['success' => true, 'status' => 'deleted_quarantine_cleanup_failed'];
    }
    return ['success' => true, 'status' => 'deleted'];
}

function e3dcRetentionStreamPrefixMatches($left, $right, $length) {
    if (!is_resource($left) || !is_resource($right)) return false;
    $remaining = max(0, (int)$length);
    if (!@rewind($left) || !@rewind($right)) return false;
    while ($remaining > 0) {
        $chunkSize = min(1024 * 1024, $remaining);
        $leftChunk = @fread($left, $chunkSize);
        $rightChunk = @fread($right, $chunkSize);
        if (!is_string($leftChunk)
            || !is_string($rightChunk)
            || $leftChunk === ''
            || strlen($leftChunk) !== strlen($rightChunk)
            || !hash_equals($leftChunk, $rightChunk)) {
            return false;
        }
        $remaining -= strlen($leftChunk);
    }
    return true;
}

function e3dcRetentionAppendStreamFully($target, $source, $offset, $length) {
    if (!is_resource($target) || !is_resource($source)) return false;
    $offset = max(0, (int)$offset);
    $remaining = max(0, (int)$length);
    if (@fseek($target, $offset, SEEK_SET) !== 0
        || @fseek($source, $offset, SEEK_SET) !== 0) {
        return false;
    }
    while ($remaining > 0) {
        $chunk = @fread($source, min(1024 * 1024, $remaining));
        if (!is_string($chunk) || $chunk === '') return false;
        $written = 0;
        $chunkLength = strlen($chunk);
        while ($written < $chunkLength) {
            $count = @fwrite($target, substr($chunk, $written));
            if ($count === false || $count <= 0) return false;
            $written += $count;
        }
        $remaining -= $chunkLength;
    }
    return true;
}

function e3dcRetentionCountStreamRows($handle) {
    if (!is_resource($handle) || !@rewind($handle)) return null;
    $rows = 0;
    while (($line = @fgets($handle)) !== false) $rows++;
    return @feof($handle) ? $rows : null;
}

function e3dcConfigBackupClassification($name) {
    $name = basename((string)$name);
    $result = [
        'class' => 'unknown',
        'timestamp' => null,
        'reason' => 'unclassified_name',
    ];

    if (preg_match('/(?:^|[._-])(manual|manuell|protected|geschuetzt|saved)(?:[._-]|$)/i', $name)) {
        $result['class'] = 'manual';
        $result['reason'] = 'explicit_manual_or_protected_marker';
        return $result;
    }
    if (preg_match('/(?:^|[._-])(migration|pre_wbchar6_compat_migration)(?:[._-]|$)/i', $name)) {
        $result['class'] = 'migration';
        $result['reason'] = 'migration_backup';
        return $result;
    }

    $matches = [];
    $knownAutomatic = preg_match(
        '/^e3dc\.config_(\d{8})_(\d{6})(?:_[A-Za-z0-9_-]+)?\.txt$/',
        $name,
        $matches
    ) === 1;
    if (!$knownAutomatic) {
        $knownAutomatic = preg_match(
            '/^e3dc_v4_(\d{8})_(\d{6})(?:_[A-Za-z0-9_-]+)?\.json(?:\.bak)?$/',
            $name,
            $matches
        ) === 1;
    }
    if (!$knownAutomatic) return $result;

    $date = DateTimeImmutable::createFromFormat(
        '!Ymd His',
        (string)$matches[1] . ' ' . (string)$matches[2]
    );
    $errors = DateTimeImmutable::getLastErrors();
    if (!$date instanceof DateTimeImmutable
        || (is_array($errors) && (((int)$errors['warning_count']) > 0 || ((int)$errors['error_count']) > 0))) {
        $result['reason'] = 'invalid_embedded_timestamp';
        return $result;
    }
    $result['class'] = 'automatic';
    $result['timestamp'] = $date->format('YmdHis');
    $result['reason'] = 'known_automatic_schema';
    return $result;
}

function e3dcPruneAutomaticConfigBackups($backupDir, $limit = 20, $options = []) {
    $backupDir = rtrim((string)$backupDir, '/');
    $limit = max(1, (int)$limit);
    $dryRun = !empty($options['dry_run']);
    $report = [
        'status' => 'ok',
        'limit' => $limit,
        'automatic' => 0,
        'manual' => 0,
        'migration' => 0,
        'unknown' => 0,
        'unsafe' => 0,
        'deleted' => [],
        'would_delete' => [],
        'errors' => [],
    ];
    if (!is_dir($backupDir) || is_link($backupDir)) {
        $report['status'] = 'backup_directory_invalid';
        return $report;
    }

    $entries = @glob($backupDir . '/*');
    if (!is_array($entries)) {
        $report['status'] = 'backup_inventory_failed';
        return $report;
    }
    $automatic = [];
    foreach ($entries as $path) {
        if (is_dir($path) && !is_link($path)) continue;
        $bound = e3dcRetentionRegularFileSnapshot($path);
        if (!is_array($bound)) {
            $report['unsafe']++;
            continue;
        }
        $classification = e3dcConfigBackupClassification(basename($path));
        $class = (string)$classification['class'];
        if (!array_key_exists($class, $report)) $class = 'unknown';
        $report[$class]++;
        if ($class !== 'automatic') continue;
        $automatic[] = [
            'path' => $path,
            'name' => basename($path),
            'timestamp' => (string)$classification['timestamp'],
            'bound' => $bound,
        ];
    }

    usort($automatic, static function($left, $right) {
        $byTime = strcmp((string)$left['timestamp'], (string)$right['timestamp']);
        return $byTime !== 0 ? $byTime : strcmp((string)$left['name'], (string)$right['name']);
    });
    $removeCount = max(0, count($automatic) - $limit);
    foreach (array_slice($automatic, 0, $removeCount) as $entry) {
        if ($dryRun) {
            $report['would_delete'][] = (string)$entry['name'];
            continue;
        }
        $deleted = e3dcRetentionDeleteBoundFile($entry['path'], $entry['bound'], $options);
        if (empty($deleted['success'])) {
            $report['errors'][] = (string)$entry['name'] . ':' . (string)$deleted['status'];
            continue;
        }
        $report['deleted'][] = (string)$entry['name'];
        if ((string)$deleted['status'] !== 'deleted') {
            $report['errors'][] = (string)$entry['name'] . ':' . (string)$deleted['status'];
        }
    }
    if ($report['errors'] !== []) $report['status'] = 'partial_cleanup';
    return $report;
}

function e3dcConfigRetentionWarning($report) {
    if (!is_array($report)) {
        return 'Das Ergebnis der automatischen Konfigurationsbereinigung ist nicht lesbar.';
    }
    $status = (string)($report['status'] ?? 'unknown');
    if ($status === 'ok') return null;
    $limit = max(1, (int)($report['limit'] ?? 20));
    return 'Die neue Konfigurationssicherung ist bestätigt; automatische Altstände '
        . 'konnten jedoch nicht vollständig auf ' . $limit . ' begrenzt werden ('
        . $status . '). Manuelle, Migrations- und unbekannte Sicherungen bleiben erhalten.';
}

function e3dcHistoryDateFromName($name) {
    if (!preg_match('/^history_(\d{4}-\d{2}-\d{2})\.txt$/', basename((string)$name), $matches)) {
        return null;
    }
    $date = DateTimeImmutable::createFromFormat('!Y-m-d', (string)$matches[1]);
    $errors = DateTimeImmutable::getLastErrors();
    if (!$date instanceof DateTimeImmutable
        || (is_array($errors) && (((int)$errors['warning_count']) > 0 || ((int)$errors['error_count']) > 0))
        || $date->format('Y-m-d') !== (string)$matches[1]) {
        return null;
    }
    return $date;
}

function e3dcHistoryArchivedDates($dbPath, $options = []) {
    $dbPath = (string)$dbPath;
    if (!class_exists('SQLite3') || !is_file($dbPath) || is_link($dbPath)) return null;
    $bound = e3dcRetentionRegularFileSnapshot($dbPath);
    if (!is_array($bound)) return null;

    $boundHandle = @fopen($dbPath, 'rb');
    if (!is_resource($boundHandle)) return null;
    $locked = false;
    $db = null;
    $query = null;
    try {
        $locked = @flock($boundHandle, LOCK_SH);
        if (!$locked || !e3dcRetentionOpenedMatchesNamedFile($boundHandle, $dbPath, $bound)) {
            return null;
        }
        $descriptorsBeforeOpen = e3dcRetentionProcessFileDescriptorSnapshots();
        if (!is_array($descriptorsBeforeOpen)) return null;

        $db = new SQLite3($dbPath, SQLITE3_OPEN_READONLY);
        $descriptorsAfterOpen = e3dcRetentionProcessFileDescriptorSnapshots();
        $databaseDescriptors = e3dcRetentionNewBoundDescriptors(
            $descriptorsBeforeOpen,
            $descriptorsAfterOpen,
            $bound
        );
        if (!is_array($databaseDescriptors)
            || $databaseDescriptors === []
            || !e3dcRetentionOpenedMatchesNamedFile($boundHandle, $dbPath, $bound)) {
            return null;
        }

        $query = $db->query("SELECT date FROM daily_stats WHERE date GLOB '????-??-??'");
        if ($query === false) return null;
        $dates = [];
        while (($row = $query->fetchArray(SQLITE3_ASSOC)) !== false) {
            $date = e3dcHistoryDateFromName('history_' . (string)($row['date'] ?? '') . '.txt');
            if ($date instanceof DateTimeImmutable) $dates[$date->format('Y-m-d')] = true;
        }
        $descriptorsAfterQuery = e3dcRetentionProcessFileDescriptorSnapshots();
        if (!e3dcRetentionHasStillBoundDescriptor($databaseDescriptors, $descriptorsAfterQuery, $bound)
            || !e3dcRetentionOpenedMatchesNamedFile($boundHandle, $dbPath, $bound)) {
            return null;
        }
        return $dates;
    } catch (Throwable $error) {
        return null;
    } finally {
        if (is_object($query) && method_exists($query, 'finalize')) @$query->finalize();
        if (is_object($db) && method_exists($db, 'close')) @$db->close();
        if ($locked) @flock($boundHandle, LOCK_UN);
        @fclose($boundHandle);
    }
}

function e3dcPruneDetailedHistory($backupDir, $dbPath, $retentionDays = 30, $options = []) {
    $backupDir = rtrim((string)$backupDir, '/');
    $retentionDays = max(1, (int)$retentionDays);
    $todayText = (string)($options['today'] ?? date('Y-m-d'));
    $today = e3dcHistoryDateFromName('history_' . $todayText . '.txt');
    $dryRun = !empty($options['dry_run']);
    $report = [
        'status' => 'ok',
        'retention_days' => $retentionDays,
        'cutoff' => null,
        'deleted' => [],
        'would_delete' => [],
        'kept_without_database_row' => [],
        'unknown' => [],
        'unsafe' => [],
        'errors' => [],
    ];
    if (!is_dir($backupDir) || is_link($backupDir) || !$today instanceof DateTimeImmutable) {
        $report['status'] = 'history_directory_or_date_invalid';
        return $report;
    }
    // Der heutige Kalendertag zählt zur Aufbewahrung. Bei 30 Tagen bleiben
    // damit exakt heute und die 29 vorherigen Datumsdateien erhalten.
    $cutoff = $today->modify('-' . ($retentionDays - 1) . ' days');
    $report['cutoff'] = $cutoff->format('Y-m-d');
    $archivedDates = array_key_exists('archived_dates', $options)
        ? $options['archived_dates']
        : e3dcHistoryArchivedDates($dbPath, $options);
    if (!is_array($archivedDates)) {
        $report['status'] = 'longterm_archive_unconfirmed';
        return $report;
    }
    $archivedDates = array_fill_keys(array_map('strval', array_keys($archivedDates)), true);

    $entries = @glob($backupDir . '/history_*.txt');
    if (!is_array($entries)) {
        $report['status'] = 'history_inventory_failed';
        return $report;
    }
    foreach ($entries as $path) {
        $name = basename($path);
        $date = e3dcHistoryDateFromName($name);
        if (!$date instanceof DateTimeImmutable) {
            $report['unknown'][] = $name;
            continue;
        }
        if ($date >= $cutoff) continue;
        $bound = e3dcRetentionRegularFileSnapshot($path);
        if (!is_array($bound)) {
            $report['unsafe'][] = $name;
            continue;
        }
        $dateText = $date->format('Y-m-d');
        if (empty($archivedDates[$dateText])) {
            $report['kept_without_database_row'][] = $name;
            continue;
        }
        if ($dryRun) {
            $report['would_delete'][] = $name;
            continue;
        }
        $deleted = e3dcRetentionDeleteBoundFile($path, $bound, $options);
        if (empty($deleted['success'])) {
            $report['errors'][] = $name . ':' . (string)$deleted['status'];
            continue;
        }
        $report['deleted'][] = $name;
        if ((string)$deleted['status'] !== 'deleted') {
            $report['errors'][] = $name . ':' . (string)$deleted['status'];
        }
    }
    if ($report['errors'] !== []) $report['status'] = 'partial_cleanup';
    return $report;
}

function e3dcArchiveHistoryDay($source, $backupDir, $dayText, $options = []) {
    $source = (string)$source;
    $backupDir = rtrim((string)$backupDir, '/');
    $day = e3dcHistoryDateFromName('history_' . (string)$dayText . '.txt');
    $fileMode = (int)($options['mode'] ?? 0660) & 0777;
    if (!$day instanceof DateTimeImmutable) return ['success' => false, 'status' => 'archive_date_invalid'];
    if (!in_array($fileMode, [0660, 0664], true)) return ['success' => false, 'status' => 'archive_mode_invalid'];
    if (!is_dir($backupDir) || is_link($backupDir)) return ['success' => false, 'status' => 'archive_directory_invalid'];
    $sourceBound = e3dcRetentionRegularFileSnapshot($source);
    if (!is_array($sourceBound)) return ['success' => false, 'status' => 'archive_source_invalid'];

    $target = $backupDir . '/history_' . $day->format('Y-m-d') . '.txt';
    try {
        $random = bin2hex(random_bytes(8));
    } catch (Throwable $error) {
        return ['success' => false, 'status' => 'archive_name_failed'];
    }
    $temporary = $backupDir . '/.history_' . $day->format('Y-m-d') . '.' . $random . '.tmp';
    $input = @fopen($source, 'rb');
    if (!is_resource($input)) {
        return ['success' => false, 'status' => 'archive_open_failed'];
    }
    if (!e3dcRetentionOpenedMatchesNamedFile($input, $source, $sourceBound)) {
        @fclose($input);
        return ['success' => false, 'status' => 'archive_source_drift'];
    }
    $output = @fopen($temporary, 'x+b');
    if (!is_resource($output)) {
        if (is_resource($input)) @fclose($input);
        if (file_exists($temporary) || is_link($temporary)) @unlink($temporary);
        return ['success' => false, 'status' => 'archive_open_failed'];
    }

    $rows = 0;
    $ok = true;
    while (($line = fgets($input)) !== false) {
        $row = @json_decode($line, true);
        $timestamp = is_array($row) ? (string)($row['ts'] ?? '') : '';
        if (!preg_match('/^' . preg_quote($day->format('Y-m-d'), '/') . '(?:T| )/', $timestamp)) continue;
        if (@fwrite($output, $line) !== strlen($line)) {
            $ok = false;
            break;
        }
        $rows++;
    }
    if (!feof($input)) $ok = false;
    $sourceStable = e3dcRetentionOpenedMatchesNamedFile($input, $source, $sourceBound);
    @fflush($output);
    if (function_exists('fsync')) @fsync($output);
    @fclose($input);
    @fclose($output);
    $sourceNamedStable = e3dcRetentionSameFileGeneration($source, $sourceBound);

    if (!$ok
        || !$sourceStable
        || !$sourceNamedStable
        || $rows < 1) {
        @unlink($temporary);
        return [
            'success' => false,
            'status' => !$ok
                ? 'archive_write_failed'
                : ((!$sourceStable || !$sourceNamedStable)
                    ? 'archive_source_drift'
                    : 'archive_empty'),
            'rows' => $rows,
        ];
    }
    $group = array_key_exists('group', $options) ? $options['group'] : 'www-data';
    if ($group !== null && !@chgrp($temporary, $group)) {
        @unlink($temporary);
        return ['success' => false, 'status' => 'archive_group_failed', 'rows' => $rows];
    }
    if (!@chmod($temporary, $fileMode)) {
        @unlink($temporary);
        return ['success' => false, 'status' => 'archive_mode_failed', 'rows' => $rows];
    }
    $temporaryBound = e3dcRetentionRegularFileSnapshot($temporary);
    $prepared = is_array($temporaryBound) ? @fopen($temporary, 'rb') : false;
    if (!is_array($temporaryBound) || !is_resource($prepared)) {
        if (is_resource($prepared)) @fclose($prepared);
        @unlink($temporary);
        return ['success' => false, 'status' => 'archive_publish_failed', 'rows' => $rows];
    }

    try {
        clearstatcache(true, $target);
        $targetExists = file_exists($target) || is_link($target);
        if (!$targetExists) {
            // link() besitzt im Gegensatz zu rename() eine echte No-Clobber-
            // Semantik: Ein zwischenzeitlich angelegtes Ziel wird nie ersetzt.
            if (!@link($temporary, $target)) {
                return ['success' => false, 'status' => 'archive_target_drift', 'rows' => $rows];
            }
            if (!@unlink($temporary)) {
                return ['success' => false, 'status' => 'archive_publish_unconfirmed', 'rows' => $rows];
            }
            if (!e3dcRetentionSameFileGeneration($target, $temporaryBound)) {
                return ['success' => false, 'status' => 'archive_readback_failed', 'rows' => $rows];
            }
            return [
                'success' => true,
                'status' => 'archive_confirmed',
                'path' => $target,
                'rows' => $rows,
                'bytes' => (int)$temporaryBound['size'],
            ];
        }

        $targetBound = e3dcRetentionRegularFileSnapshot($target);
        if (!is_array($targetBound)) {
            return ['success' => false, 'status' => 'archive_target_invalid', 'rows' => $rows];
        }
        $existing = @fopen($target, 'r+b');
        if (!is_resource($existing)) {
            return ['success' => false, 'status' => 'archive_target_open_failed', 'rows' => $rows];
        }
        try {
            if (!@flock($existing, LOCK_EX)) {
                return ['success' => false, 'status' => 'archive_target_lock_failed', 'rows' => $rows];
            }
            try {
                if (!e3dcRetentionOpenedMatchesNamedFile($existing, $target, $targetBound)) {
                    return ['success' => false, 'status' => 'archive_target_drift', 'rows' => $rows];
                }
                if (!e3dcRetentionOpenedMatchesNamedFile($existing, $target, $targetBound)) {
                    return ['success' => false, 'status' => 'archive_target_drift', 'rows' => $rows];
                }

                $targetSize = (int)$targetBound['size'];
                $preparedSize = (int)$temporaryBound['size'];
                $commonSize = min($targetSize, $preparedSize);
                if (!e3dcRetentionStreamPrefixMatches($existing, $prepared, $commonSize)) {
                    return ['success' => false, 'status' => 'archive_target_conflict', 'rows' => $rows];
                }
                if (!e3dcRetentionOpenedMatchesNamedFile($existing, $target, $targetBound)) {
                    return ['success' => false, 'status' => 'archive_target_drift', 'rows' => $rows];
                }

                if ($targetSize >= $preparedSize) {
                    $existingRows = e3dcRetentionCountStreamRows($existing);
                    if (!is_int($existingRows)
                        || !e3dcRetentionOpenedMatchesNamedFile($existing, $target, $targetBound)) {
                        return ['success' => false, 'status' => 'archive_target_drift', 'rows' => $rows];
                    }
                    return [
                        'success' => true,
                        'status' => $targetSize === $preparedSize
                            ? 'archive_unchanged'
                            : 'archive_existing_preserved',
                        'path' => $target,
                        'rows' => $existingRows,
                        'bytes' => $targetSize,
                    ];
                }

                $suffixLength = $preparedSize - $targetSize;
                if (!e3dcRetentionAppendStreamFully(
                    $existing,
                    $prepared,
                    $targetSize,
                    $suffixLength
                )
                    || !@fflush($existing)
                    || (function_exists('fsync') && !@fsync($existing))) {
                    return ['success' => false, 'status' => 'archive_extension_failed', 'rows' => $rows];
                }
                clearstatcache(true, $target);
                $extendedBound = e3dcRetentionOpenedRegularFileSnapshot($existing);
                if (!is_array($extendedBound)
                    || (int)$extendedBound['dev'] !== (int)$targetBound['dev']
                    || (int)$extendedBound['ino'] !== (int)$targetBound['ino']
                    || (int)$extendedBound['size'] !== $preparedSize
                    || !e3dcRetentionOpenedMatchesNamedFile($existing, $target, $extendedBound)
                    || !e3dcRetentionStreamPrefixMatches($existing, $prepared, $preparedSize)) {
                    return ['success' => false, 'status' => 'archive_extension_unconfirmed', 'rows' => $rows];
                }
                return [
                    'success' => true,
                    'status' => 'archive_extended',
                    'path' => $target,
                    'rows' => $rows,
                    'bytes' => $preparedSize,
                ];
            } finally {
                @flock($existing, LOCK_UN);
            }
        } finally {
            @fclose($existing);
        }
    } finally {
        @fclose($prepared);
        if (file_exists($temporary) || is_link($temporary)) @unlink($temporary);
    }
}
