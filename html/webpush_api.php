<?php
// webpush_api.php - Endpoint for PWA Push Subscriptions
header('Content-Type: application/json');
require_once 'helpers.php';

$action = $_GET['action'] ?? $_POST['action'] ?? '';
if (in_array($action, ['get_vapid', 'subscribe', 'test_push'], true)) {
    e3dcRequirePostMutation(true);
} else {
    requireWebAuth(true);
}
$paths = getInstallPaths();
$installRoot = !empty($paths['valid']) ? @realpath(rtrim((string)$paths['install_path'], '/')) : false;

// Helper to base64url encode
function base64url_encode($data) {
    return rtrim(strtr(base64_encode($data), '+/', '-_'), '=');
}

if ($action === 'get_vapid') {
    $config = loadE3dcConfig();
    $pub = $config['config']['push_vapid_public'] ?? '';
    $private = $config['config']['push_vapid_private'] ?? '';

    // Auto-Generate VAPID if missing
    if (empty($pub) || empty($private)) {
        $scriptPath = $installRoot !== false ? $installRoot . '/Installer/generate_vapid.py' : '';
        $scriptReal = $scriptPath !== '' ? @realpath($scriptPath) : false;
        $python = e3dcGetTrustedPythonInterpreter();
        if ($python === null || $scriptReal === false || is_link($scriptPath)
            || !str_starts_with($scriptReal, rtrim((string)$installRoot, '/') . '/Installer/')) {
            http_response_code(503);
            echo json_encode(['error' => 'VAPID-Helfer ist im gebundenen Installationspfad nicht sicher verfügbar. Bitte Installation oder Rechte reparieren.']);
            exit;
        }
        $result = e3dcRunArgvProcess([$python, $scriptReal], 10.0, ['cwd' => dirname($scriptReal), 'max_output_bytes' => 16384]);
        $generated = @json_decode(trim((string)($result['stdout'] ?? '')), true);
        $generatedPublic = is_array($generated) ? trim((string)($generated['public'] ?? '')) : '';
        $generatedPrivate = is_array($generated) ? trim((string)($generated['private'] ?? '')) : '';
        $keysValid = !empty($result['success'])
            && !empty($generated['success'])
            && preg_match('/^[A-Za-z0-9_-]{80,100}$/', $generatedPublic)
            && preg_match('/^[A-Za-z0-9_-]{40,50}$/', $generatedPrivate);
        if (!$keysValid || !saveE3dcConfigValues([
            'push_vapid_public' => $generatedPublic,
            'push_vapid_private' => $generatedPrivate,
        ])) {
            http_response_code(500);
            echo json_encode(['error' => 'VAPID-Schlüssel konnten nicht sicher gespeichert werden. Bitte im Installationscenter „Rechte reparieren“ ausführen.']);
            exit;
        }
        $config = loadE3dcConfig();
        $pub = $config['config']['push_vapid_public'] ?? '';
        $private = $config['config']['push_vapid_private'] ?? '';
        if (!hash_equals($generatedPublic, (string)$pub) || !hash_equals($generatedPrivate, (string)$private)) {
            http_response_code(500);
            echo json_encode(['error' => 'VAPID-Schlüssel wurden gespeichert, konnten aber nicht eindeutig zurückgelesen werden.']);
            exit;
        }
    }
    
    echo json_encode(['public_key' => $pub]);
    exit;
}

if ($action === 'subscribe') {
    $rawBody = file_get_contents('php://input');
    $data = is_string($rawBody) && strlen($rawBody) <= 65536 ? json_decode($rawBody, true) : null;
    $endpoint = is_array($data) ? trim((string)($data['endpoint'] ?? '')) : '';
    $p256dh = is_array($data) ? trim((string)($data['keys']['p256dh'] ?? '')) : '';
    $auth = is_array($data) ? trim((string)($data['keys']['auth'] ?? '')) : '';
    if (!is_array($data) || !filter_var($endpoint, FILTER_VALIDATE_URL)
        || !str_starts_with(strtolower($endpoint), 'https://') || $p256dh === '' || $auth === '') {
        http_response_code(400);
        echo json_encode(['success' => false, 'error' => 'Push-Abonnement ist unvollständig oder ungültig.']);
        exit;
    }

    $dbPath = '/var/www/html/data/e3dc_stats.db';
    if (!is_dir(dirname($dbPath))) {
        http_response_code(500);
        echo json_encode(['success' => false, 'error' => 'Datenverzeichnis fehlt. Bitte Installation oder Rechte reparieren.']);
        exit;
    }
    
    try {
        $db = new PDO('sqlite:' . $dbPath);
        $db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
        
        // Init table
        $db->exec("CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint TEXT UNIQUE,
            p256dh TEXT,
            auth TEXT,
            device_name TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )");

        $stmt = $db->prepare("INSERT OR REPLACE INTO push_subscriptions (endpoint, p256dh, auth, device_name) VALUES (?, ?, ?, ?)");
        $stmt->execute([
            $endpoint,
            $p256dh,
            $auth,
            substr((string)($data['device_name'] ?? 'PWA Client'), 0, 255)
        ]);

        echo json_encode(['success' => true]);
    } catch (Exception $e) {
        error_log('WebPush-Abonnement konnte nicht gespeichert werden.');
        http_response_code(500);
        echo json_encode(['success' => false, 'error' => 'Push-Abonnement konnte nicht gespeichert werden. Bitte Datenbankrechte prüfen.']);
    }
    exit;
}

if ($action === 'test_push') {
    $scriptPath = $installRoot !== false ? $installRoot . '/Installer/send_push.py' : '';
    $scriptReal = $scriptPath !== '' ? @realpath($scriptPath) : false;
    $python = e3dcGetTrustedPythonInterpreter();
    if ($python === null || $scriptReal === false || is_link($scriptPath)
        || !str_starts_with($scriptReal, rtrim((string)$installRoot, '/') . '/Installer/')) {
        http_response_code(503);
        echo json_encode(['success' => false, 'error' => 'Push-Helfer ist im gebundenen Installationspfad nicht sicher verfügbar.']);
        exit;
    }
    $result = e3dcRunArgvProcess(
        [$python, $scriptReal, 'E3DC-Control Testnachricht', 'Die Push-Schnittstelle wurde erfolgreich verbunden!'],
        20.0,
        ['cwd' => dirname($scriptReal), 'max_output_bytes' => 32768]
    );
    $payload = @json_decode(trim((string)($result['stdout'] ?? '')), true);
    if (empty($result['success']) || !is_array($payload) || empty($payload['success'])) {
        http_response_code(500);
        echo json_encode(['success' => false, 'error' => 'Testnachricht wurde nicht bestätigt. Bitte Push-Konfiguration und registrierte Geräte prüfen.']);
        exit;
    }
    echo json_encode(['success' => true, 'count' => max(0, (int)($payload['count'] ?? 0))]);
    exit;
}

http_response_code(400);
echo json_encode(['error' => 'Unknown action']);
