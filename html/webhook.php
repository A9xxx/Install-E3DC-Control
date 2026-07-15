<?php
// /var/www/html/webhook.php
// Behandelt Action-Buttons aus Push-Notifikationen
require_once 'helpers.php';
header('Content-Type: application/json');

$action = $_GET['action'] ?? '';
if (!$action) {
    echo json_encode(['error' => 'No action specified']);
    exit;
}
requireWebAuth(true);

$paths = getInstallPaths();
$base_path = rtrim($paths['install_path'], '/') . '/';
$confData = loadE3dcConfig($base_path);
// config_file nicht mehr benoetigt - Schreiben erfolgt via V4 JSON

// Ermittle den Pfad zur wallbox.txt (nur fuer Legacy WB-Steuerung)
$wallbox_file = $base_path . 'e3dc.wallbox.txt';
if (isset($confData['config']['e3dcwallboxtxt']) && !empty($confData['config']['e3dcwallboxtxt'])) {
    $e3txt = trim(trim($confData['config']['e3dcwallboxtxt']), " \t\n\r\0\x0B\"");
    if (substr($e3txt, -1) !== '/' && strpos(substr($e3txt, -4), '.txt') === false) {
        $e3txt .= '/';
    }
    if (strpos(substr($e3txt, -4), '.txt') === false) {
        $wallbox_file = $e3txt . 'e3dc.wallbox.txt';
    } else {
        $wallbox_file = $e3txt;
    }
}

// Hilfsfunktion: Schreibt Keys atomar in V4 JSON
function updateV4Config($updates) {
    $v4Path = '/var/www/html/data/e3dc_v4.json';
    $data = @json_decode(@file_get_contents($v4Path), true);
    if (!is_array($data)) $data = [];
    foreach ($updates as $k => $v) {
        $data[$k] = $v;
    }
    $json = json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES);
    if ($json === false || !e3dcWriteJsonAtomic($v4Path, $json)) return false;
    @unlink('/var/www/html/ramdisk/e3dc_config_cache.json');
    return true;
}

// Actions ausführen
if ($action === 'wb_direct_99' || $action === 'wb_direct_max') {
    @file_put_contents($wallbox_file, "99\n", LOCK_EX);
    @chmod($wallbox_file, 0666);
    echo json_encode(['success' => true, 'desc' => 'Direktes Laden (99h) aktiviert.']);
    exit;
} elseif ($action === 'wb_stop_direct') {
    @file_put_contents($wallbox_file, "0\n", LOCK_EX);
    @chmod($wallbox_file, 0666);
    echo json_encode(['success' => true, 'desc' => 'Direktsteuerung gestoppt (0h).']);
    exit;
} elseif ($action === 'wb_pv_50') {
    updateV4Config(['car_target_soc' => '50']);
    @file_put_contents($wallbox_file, "0\n", LOCK_EX);
    @chmod($wallbox_file, 0666);
    echo json_encode(['success' => true, 'desc' => 'Ziel-SoC auf 50% gesetzt, Direktsteuerung gestoppt.']);
    exit;
} elseif ($action === 'emergency_stop') {
    // Wallbox Stoppen
    @file_put_contents($wallbox_file, "0\n", LOCK_EX);
    @chmod($wallbox_file, 0666);
    // WP PV-Pause erzwingen (via V4 JSON)
    updateV4Config(['pv_pause_enable' => '1', 'wbmode' => '0']);
    echo json_encode(['success' => true, 'desc' => 'Not-Aus für Wallbox und Wärmepumpe aktiviert.']);
    exit;
} elseif ($action === 'update_now') {
    http_response_code(409);
    echo json_encode([
        'success' => false,
        'error' => 'Update per Push-Aktion ist deaktiviert. Bitte das Installationszentrum nutzen.'
    ]);
    exit;
} else {
    echo json_encode(['success' => false, 'error' => 'Unbekannte Aktion: ' . $action]);
    exit;
}
