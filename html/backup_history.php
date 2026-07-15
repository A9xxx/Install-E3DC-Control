<?php
require_once 'helpers.php';
if (PHP_SAPI !== 'cli') {
    requireWebAuth(true);
}
$paths = getInstallPaths();
// Pfad zur Live-Datei (entsprechend deiner Konfiguration)
$source = '/var/www/html/ramdisk/live_history.txt';
$backupDir = '/var/www/html/data/history_backups/';

// Datum von gestern ermitteln
$yesterday = date('Y-m-d', strtotime('yesterday'));
$target = $backupDir . 'history_' . $yesterday . '.txt';

if (!is_dir($backupDir)) {
    if (!mkdir($backupDir, 0775, true)) {
        error_log("Backup-Fehler: Verzeichnis $backupDir konnte nicht erstellt werden.");
        exit(1);
    }
    // Gruppe auf www-data setzen und Schreibrechte für die Gruppe geben
    chgrp($backupDir, 'www-data');
    chmod($backupDir, 0775);
}

if (!is_writable($backupDir)) {
    error_log("Backup-Fehler: Verzeichnis $backupDir ist nicht beschreibbar.");
    exit(1);
}

if (!file_exists($source)) {
    error_log("Backup-Fehler: Quelldatei nicht gefunden unter: $source");
    exit(1);
}

if (is_readable($source)) {
    $yesterdayMatch = '"ts":"' . $yesterday;
    $handle = fopen($source, 'r');
    $out = fopen($target, 'w');

    if ($handle && $out) {
        while (($line = fgets($handle)) !== false) {
            // Nur Zeilen schreiben, die den Zeitstempel von gestern enthalten
            if (strpos($line, $yesterdayMatch) !== false) {
                fwrite($out, $line);
            }
        }
        fclose($handle);
        fclose($out);
    }
} else {
    error_log("Backup-Fehler: Quelldatei $source ist vorhanden, aber nicht lesbar (Berechtigungen prüfen).");
}

// --- DATENBANKBASIERTE LANGZEIT-ARCHIVIERUNG AKTIV ---
// Warnung: Rohe JSON-Dateien über 30 Tage werden nun gelöscht, da die sqlite-Datenbank e3dc_stats.db
// alle für langzeit.php benötigten aggregierten Daten (CO2, Jahreswerte, etc.) permanent und platzsparend speichert!

$files = glob($backupDir . 'history_*.txt');
if ($files !== false) {
    foreach ($files as $file) {
        if (time() - filemtime($file) > 30 * 86400) {
            @unlink($file);
        }
    }
}
