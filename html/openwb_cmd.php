<?php
/**
 * openwb_cmd.php - HTTP POST Proxy zur openWB simpleAPI
 * Wird von der Wallbox.php JavaScript-Karte aufgerufen.
 * Leitet Befehle an die openWB SimpleAPI weiter und gibt JSON zurück.
 */
require_once 'helpers.php';
header('Content-Type: application/json');
requireWebAuth(true);

// Nur POST erlauben
if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['success' => false, 'message' => 'Method not allowed']);
    exit;
}

// Config laden: openWB IP
$confData = loadE3dcConfig();
$wbNativeType = strtolower(trim($confData['config']['wb_native_type'] ?? ''));

if ($wbNativeType !== 'openwb') {
    echo json_encode(['success' => false, 'message' => 'openWB nicht konfiguriert']);
    exit;
}

$wbIp = trim($confData['config']['wb_native_ip'] ?? '');
if (empty($wbIp) || $wbIp === '0.0.0.0') {
    echo json_encode(['success' => false, 'message' => 'Keine openWB IP konfiguriert']);
    exit;
}

// Parameter aus POST sammeln und validieren
$allowed_keys = ['set_chargemode', 'chargecurrent', 'chargepoint_nr', 'minimal_current', 'target_current'];
$allowed_chargemodes = ['instant', 'instant_charging', 'stop', 'pv', 'pv_charging', 'eco_charging'];

$payload_parts = [];
$requested_chargemode = null;

foreach ($allowed_keys as $key) {
    if (!isset($_POST[$key])) continue;
    $val = trim($_POST[$key]);

    if ($key === 'set_chargemode') {
        if (!in_array($val, $allowed_chargemodes, true)) {
            echo json_encode(['success' => false, 'message' => 'Ungültiger chargemode: ' . htmlspecialchars($val)]);
            exit;
        }
        $requested_chargemode = $val;
        $payload_parts[] = urlencode($key) . '=' . urlencode($val);

    } elseif (in_array($key, ['chargecurrent', 'chargepoint_nr', 'minimal_current', 'target_current'])) {
        $num = (int)$val;
        // Sicherheitsgrenzen
        if ($key === 'chargecurrent' && ($num < 0 || $num > 32)) {
            echo json_encode(['success' => false, 'message' => 'Strom ausserhalb 0-32A']);
            exit;
        }
        $payload_parts[] = urlencode($key) . '=' . $num;
    }
}

if (empty($payload_parts)) {
    echo json_encode(['success' => false, 'message' => 'Keine gültigen Parameter']);
    exit;
}

// HTTP POST an openWB
$url = "http://{$wbIp}/openWB/simpleAPI/simpleapi.php";

function openwbSimpleApiPost($url, $payload_parts, $wbIp) {
    $body = implode('&', $payload_parts);
    $ctx = stream_context_create([
        'http' => [
            'method'  => 'POST',
            'header'  => "Content-Type: application/x-www-form-urlencoded\r\nContent-Length: " . strlen($body) . "\r\n",
            'content' => $body,
            'timeout' => 5,
            'ignore_errors' => true
        ]
    ]);

    $resp = @file_get_contents($url, false, $ctx);
    if ($resp === false) {
        return ['success' => false, 'message' => "openWB ({$wbIp}) nicht erreichbar"];
    }

    $json = @json_decode($resp, true);
    if (is_array($json)) {
        if (!array_key_exists('success', $json)) {
            $json['success'] = true;
        }
        return $json;
    }

    return ['success' => true, 'message' => 'Befehl gesendet', 'raw' => substr($resp, 0, 100)];
}

$result = openwbSimpleApiPost($url, $payload_parts, $wbIp);

// Einige openWB-Versionen akzeptieren die alten Kurzwerte statt der 2.x-Namen.
// Der Wallbox-Manager nutzt denselben Fallback; die Live-Tasten müssen das auch können.
$openwbChargemodeFallbacks = [
    'instant_charging' => 'instant',
    'pv_charging' => 'pv',
];
if (($result['success'] ?? false) === false && isset($openwbChargemodeFallbacks[$requested_chargemode])) {
    $fallback_mode = $openwbChargemodeFallbacks[$requested_chargemode];
    $fallback_parts = [];
    foreach ($payload_parts as $part) {
        if (strpos($part, 'set_chargemode=') === 0) {
            $fallback_parts[] = 'set_chargemode=' . urlencode($fallback_mode);
        } else {
            $fallback_parts[] = $part;
        }
    }
    $fallback_result = openwbSimpleApiPost($url, $fallback_parts, $wbIp);
    if (($fallback_result['success'] ?? false) === true) {
        $fallback_result['message'] = trim(($fallback_result['message'] ?? 'Befehl gesendet') . ' (Fallback: ' . $fallback_mode . ')');
        $result = $fallback_result;
    }
}

echo json_encode($result);
