<?php
require_once __DIR__ . '/helpers.php';
if (PHP_SAPI !== 'cli') {
    requireWebAuth(true);
}

$source = '/var/www/html/ramdisk/live_history.txt';
$backupDir = '/var/www/html/data/history_backups';
$database = '/var/www/html/data/e3dc_stats.db';
$configBackupDir = '/var/www/html/data/config_backups';
$yesterday = date('Y-m-d', strtotime('yesterday'));
$hourly = PHP_SAPI === 'cli' && in_array('--heute', $argv ?? [], true);
$configRaw = e3dcReadRegularFileBound('/var/www/html/data/e3dc_v4.json', 4 * 1024 * 1024);
$configData = is_string($configRaw) ? @json_decode($configRaw, true) : null;
if (!is_array($configData)) {
    error_log('Backup-Fehler: Schutzmodus der Anlagenkonfiguration ist nicht sicher lesbar.');
    exit(1);
}
$historyDirMode = e3dcConfigSecretDirModeFromData($configData);
$historyFileMode = e3dcConfigSecretFileModeFromData($configData);

if (!is_dir($backupDir)) {
    if (is_link($backupDir) || !@mkdir($backupDir, $historyDirMode, true)) {
        error_log("Backup-Fehler: Verzeichnis $backupDir konnte nicht erstellt werden.");
        exit(1);
    }
    @chgrp($backupDir, 'www-data');
    @chmod($backupDir, $historyDirMode);
}

$days = $hourly ? [$yesterday, date('Y-m-d')] : [$yesterday];
$archiveFailed = false;
foreach ($days as $archiveDay) {
    $archive = e3dcArchiveHistoryDay($source, $backupDir, $archiveDay, ['mode' => $historyFileMode]);
    if (empty($archive['success'])) {
        if ($hourly && (($archive['status'] ?? '') === 'archive_empty'
            || (($archive['status'] ?? '') === 'archive_source_invalid' && !file_exists($source) && !is_link($source)))) continue;
        error_log('Backup-Fehler: Tageshistorie wurde nicht bestätigt ('
            . (string)($archive['status'] ?? 'unknown') . '). Bestehende Archive werden nicht bereinigt.');
        if (!$hourly) exit(1);
        $archiveFailed = true;
        continue;
    }
    if (!$hourly || (int)$archive['rows'] > 0 || $archive['status'] === 'archive_created') {
        echo 'Tageshistorie: ' . (string)$archive['status'] . ': ' . basename((string)$archive['path'])
            . ' (' . (int)$archive['rows'] . " neue Datensätze).\n";
    }
}
if ($hourly) exit($archiveFailed ? 1 : 0);

$historyRetention = e3dcPruneDetailedHistory($backupDir, $database, 30);
if (($historyRetention['status'] ?? '') === 'longterm_archive_unconfirmed') {
    error_log(
        'Backup-Hinweis: Langzeitdatenbank ist nicht sicher lesbar; '
        . 'alte Tageshistorien bleiben unverändert erhalten.'
    );
} elseif (($historyRetention['status'] ?? '') !== 'ok') {
    error_log(
        'Backup-Hinweis: Tageshistorien nur teilweise bereinigt ('
        . (string)($historyRetention['status'] ?? 'unknown')
        . '). Nicht bestätigte Dateien bleiben erhalten.'
    );
}
if (!empty($historyRetention['kept_without_database_row'])) {
    error_log(
        'Backup-Hinweis: '
        . count($historyRetention['kept_without_database_row'])
        . ' alte Tageshistorie(n) ohne bestätigten Langzeit-Datensatz bleiben erhalten.'
    );
}

$configRetention = e3dcPruneAutomaticConfigBackups($configBackupDir, 20);
if (($configRetention['status'] ?? '') !== 'ok'
    && ($configRetention['status'] ?? '') !== 'backup_directory_invalid') {
    error_log(
        'Backup-Hinweis: Automatische Konfigurationssicherungen nur teilweise bereinigt ('
        . (string)($configRetention['status'] ?? 'unknown')
        . '). Manuelle, Migrations- und unbekannte Dateien bleiben erhalten.'
    );
}

echo 'Tageshistorien entfernt: ' . count($historyRetention['deleted'] ?? []) . ".\n";
echo 'Automatische Konfigurationssicherungen entfernt: '
    . count($configRetention['deleted'] ?? []) . ".\n";
