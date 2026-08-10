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
// $configFile entfernt: generate_vapid.py liest V4 JSON selbst

// Helper to base64url encode
function base64url_encode($data) {
    return rtrim(strtr(base64_encode($data), '+/', '-_'), '=');
}

if ($action === 'get_vapid') {
    $config = loadE3dcConfig();
    $pub = $config['config']['push_vapid_public'] ?? '';
    
    // Auto-Generate VAPID if missing
    if (empty($pub)) {
        $homeDir = rtrim($paths['home_dir'], '/');
        $scriptPath = "$homeDir/Install/Installer/generate_vapid.py";
        if (!file_exists($scriptPath)) { $scriptPath = "$homeDir/pi/Install/Installer/generate_vapid.py"; }
        $pythonScript = escapeshellarg($scriptPath);
        $pythonCmd = escapeshellarg(getPythonInterpreter());
        exec("$pythonCmd " . $pythonScript . ' 2>&1', $out, $ret);
        $config = loadE3dcConfig();
        $pub = $config['config']['push_vapid_public'] ?? '';
        
        if (empty($pub)) {
            echo json_encode(['error' => 'Auto-Generierung fehlgeschlagen. ' . implode(" ", $out)]);
            exit;
        }
    }
    
    echo json_encode(['public_key' => $pub]);
    exit;
}

if ($action === 'subscribe') {
    $data = json_decode(file_get_contents('php://input'), true);
    if (!$data || !isset($data['endpoint'])) {
        echo json_encode(['success' => false, 'error' => 'Invalid data']);
        exit;
    }

    $dbPath = '/var/www/html/data/e3dc_stats.db';
    if (!file_exists(dirname($dbPath))) { @mkdir(dirname($dbPath), 0775, true); }
    
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
            $data['endpoint'],
            $data['keys']['p256dh'] ?? '',
            $data['keys']['auth'] ?? '',
            $data['device_name'] ?? 'PWA Client'
        ]);

        echo json_encode(['success' => true]);
    } catch (Exception $e) {
        echo json_encode(['success' => false, 'error' => $e->getMessage()]);
    }
    exit;
}

if ($action === 'test_push') {
    $homeDir = rtrim($paths['home_dir'], '/');
    $scriptPath = "$homeDir/Install/Installer/send_push.py";
    if (!file_exists($scriptPath)) { $scriptPath = "$homeDir/pi/Install/Installer/send_push.py"; }
    $pythonScript = escapeshellarg($scriptPath);
    $pythonCmd = escapeshellarg(getPythonInterpreter());
    
    // We execute the python script as a helper to send WebPush via pywebpush
    // The script will return JSON directly.
    $command = "$pythonCmd " . $pythonScript . ' ' . escapeshellarg("E3DC-Control Testnachricht") . ' ' . escapeshellarg("Die Push-Schnittstelle wurde erfolgreich verbunden!");
    exec("$command 2>&1", $out, $ret);
    
    // Output could be multiple lines or error traceback
    $responseString = implode("\n", $out);
    
    // Try to decode exactly what python printed to JSON
    $jsonValid = @json_decode($responseString, true);
    if (json_last_error() === JSON_ERROR_NONE) {
        echo $responseString;
    } else {
        // If Python crashed (e.g. pywebpush not installed)
        echo json_encode(['success' => false, 'error' => substr($responseString, 0, 500)]);
    }
    exit;
}

echo json_encode(['error' => 'Unknown action']);
