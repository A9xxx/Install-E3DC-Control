<?php
/**
 * manual_bat_cmd.php - Manueller Batterie-Override
 * Schreibt/löscht /var/www/html/ramdisk/manual_bat_override.json
 * Wird per AJAX aus config_editor.php aufgerufen.
 */
if (session_status() === PHP_SESSION_NONE) session_start();
require_once __DIR__ . '/helpers.php';
header('Content-Type: application/json');
requireWebAuth(true);

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    http_response_code(405);
    echo json_encode(['ok' => false, 'msg' => 'Method not allowed']);
    exit;
}
e3dcRequireCsrfToken(true);

$override_file = '/var/www/html/ramdisk/manual_bat_override.json';
$action = $_POST['action'] ?? '';

function manualBatClampMaxAgeHours($value) {
    if (is_string($value)) {
        $value = str_replace(',', '.', trim($value));
    }
    $hours = is_numeric($value) ? floatval($value) : 12.0;
    return max(1.0, min(24.0, $hours));
}

function manualBatOverrideMaxAgeHours() {
    foreach (['/var/www/html/data/e3dc_v4.json'] as $path) {
        if (!is_readable($path)) continue;
        $data = json_decode(file_get_contents($path), true);
        if (!is_array($data) || !array_key_exists('storage_manual_override_max_age_h', $data)) continue;
        return manualBatClampMaxAgeHours($data['storage_manual_override_max_age_h']);
    }
    return 12.0;
}

function manualBatFormatHours($hours) {
    $text = rtrim(rtrim(number_format((float)$hours, 2, '.', ''), '0'), '.');
    return $text === '' ? '12' : $text;
}

$allowed = ['charge', 'discharge', 'auto'];
if (!in_array($action, $allowed)) {
    http_response_code(400);
    echo json_encode(['ok' => false, 'msg' => 'Unbekannte Aktion: ' . htmlspecialchars($action)]);
    exit;
}

if ($action === 'auto') {
    // Override löschen = Automatik
    $removed = e3dcRemoveRuntimeCommandFile($override_file);
    if (empty($removed['success'])) {
        http_response_code(500);
        echo json_encode([
            'ok' => false,
            'msg' => (string)($removed['message'] ?? 'Der manuelle Batterie-Override konnte nicht sicher entfernt werden.'),
        ], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        exit;
    }
    echo json_encode(['ok' => true, 'msg' => 'Automatik aktiv — manueller Override entfernt.']);
    exit;
}

$target_soc = intval($_POST['target_soc'] ?? ($action === 'charge' ? 100 : 10));
$target_soc = max(5, min(100, $target_soc));
$max_age_h = manualBatOverrideMaxAgeHours();
$max_age_label = manualBatFormatHours($max_age_h);
$now = time();

$data = [
    'mode'       => $action,
    'target_soc' => $target_soc,
    'ts'         => $now,
    'expires_ts' => $now + (int)round($max_age_h * 3600),
    'max_age_h'  => $max_age_h,
    'set_by'     => 'manual_bat_cmd.php'
];

$encoded = json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
$published = is_string($encoded)
    ? e3dcPublishRuntimeCommandFile($override_file, $encoded . "\n", 0664, '.manual_bat_override.')
    : ['success' => false, 'message' => 'Der Batterie-Override konnte nicht kodiert werden.'];
if (empty($published['success'])) {
    http_response_code(500);
    echo json_encode([
        'ok' => false,
        'msg' => (string)($published['message'] ?? 'Der Batterie-Override konnte nicht sicher gespeichert werden.'),
    ], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

$label = $action === 'charge' ? 'Laden' : 'Entladen';
echo json_encode(['ok' => true, 'msg' => "Manuelles {$label} aktiviert (Ziel: {$target_soc}%, max. {$max_age_label}h). Storage Manager übernimmt im nächsten Zyklus."]);
