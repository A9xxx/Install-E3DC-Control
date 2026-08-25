<?php
require_once __DIR__ . '/helpers.php';

function e3dcSendTelegramTestMessage(array $config) {
    $token = trim((string)($config['telegram_token'] ?? ''));
    $chatIdRaw = trim((string)($config['telegram_chat_id'] ?? ''));
    $deviceName = trim((string)($config['telegram_device_name'] ?? 'E3DC-Control'));
    if ($token === '' || $chatIdRaw === '') {
        return [
            'success' => false,
            'message' => 'Telegram Bot Token und Chat ID müssen ausgefüllt sein.',
        ];
    }
    if (!function_exists('curl_init')) {
        return [
            'success' => false,
            'message' => 'Die PHP-cURL-Erweiterung für Telegram ist nicht verfügbar.',
        ];
    }

    $chatIds = array_values(array_filter(array_map('trim', explode(',', $chatIdRaw)), static function($chatId) {
        return $chatId !== '';
    }));
    if ($chatIds === [] || count($chatIds) > 20) {
        return [
            'success' => false,
            'message' => 'Es muss mindestens eine und höchstens 20 Chat IDs geben.',
        ];
    }

    $message = '✅ Telegram-Test von ' . ($deviceName !== '' ? $deviceName : 'E3DC-Control')
        . ' (' . date('d.m.Y H:i:s') . ')';
    $url = 'https://api.telegram.org/bot' . $token . '/sendMessage';
    $sent = 0;
    foreach ($chatIds as $chatId) {
        $handle = curl_init();
        curl_setopt($handle, CURLOPT_URL, $url);
        curl_setopt($handle, CURLOPT_POST, true);
        curl_setopt($handle, CURLOPT_POSTFIELDS, http_build_query([
            'chat_id' => $chatId,
            'text' => $message,
        ]));
        curl_setopt($handle, CURLOPT_RETURNTRANSFER, true);
        curl_setopt($handle, CURLOPT_CONNECTTIMEOUT, 5);
        curl_setopt($handle, CURLOPT_TIMEOUT, 10);
        if (defined('CURLOPT_PROTOCOLS') && defined('CURLPROTO_HTTPS')) {
            curl_setopt($handle, CURLOPT_PROTOCOLS, CURLPROTO_HTTPS);
        }
        $response = curl_exec($handle);
        $curlError = curl_errno($handle);
        $httpStatus = (int)curl_getinfo($handle, CURLINFO_HTTP_CODE);
        curl_close($handle);
        $payload = is_string($response) ? json_decode($response, true) : null;
        if ($curlError !== 0 || $httpStatus < 200 || $httpStatus >= 300 || !is_array($payload) || ($payload['ok'] ?? false) !== true) {
            return [
                'success' => false,
                'message' => $curlError !== 0
                    ? 'Telegram ist derzeit nicht erreichbar.'
                    : 'Telegram hat die Testnachricht abgelehnt (HTTP ' . ($httpStatus > 0 ? $httpStatus : 502) . ').',
            ];
        }
        $sent++;
    }

    return [
        'success' => true,
        'count' => $sent,
        'message' => $sent === 1
            ? 'Telegram-Testnachricht wurde bestätigt.'
            : $sent . ' Telegram-Testnachrichten wurden bestätigt.',
    ];
}

$telegramScript = realpath((string)($_SERVER['SCRIPT_FILENAME'] ?? ''));
if ($telegramScript === false || $telegramScript !== realpath(__FILE__)) {
    return;
}

if (PHP_SAPI !== 'cli') {
    requireWebAuth(true);
    if (strtoupper((string)($_SERVER['REQUEST_METHOD'] ?? 'GET')) !== 'POST') {
        http_response_code(405);
        header('Allow: POST');
        exit('Telegram-Versand ist nur als bestätigte POST-Aktion zulässig.');
    }
    e3dcRequireCsrfToken(true);
}

$paths = getInstallPaths();
$confRes = loadE3dcConfig();
$config = $confRes['config'] ?? [];

$token = $config['telegram_token'] ?? '';
$chat_id = $config['telegram_chat_id'] ?? '';
$deviceName = $config['telegram_device_name'] ?? 'E3DC-Control';

if (empty($token) || empty($chat_id)) {
    exit(0);
}

// SLAVE-BUG FIX: Nichts senden, wenn wir im Standby-Modus sind!
$ha_mode = strtolower($config['ha_mode'] ?? 'off');
if ($ha_mode === 'slave') {
    $haFile = '/var/www/html/ramdisk/ha_status.json';
    if (file_exists($haFile)) {
        $haData = @json_decode(file_get_contents($haFile), true);
        if ($haData && isset($haData['state']) && $haData['state'] !== 'failover') {
            exit(0);
        }
    }
}

// Holen von Temperatur und Uptime
$uptime = @exec('uptime -p');
if (!$uptime) {
    if (file_exists('/proc/uptime')) {
        $uptime_seconds = (int)floatval(file_get_contents('/proc/uptime'));
        $uptime = "up " . floor($uptime_seconds / 3600) . " hours";
    } else {
         $uptime = "N/A";
    }
}

$temp = '';
if (file_exists('/sys/class/thermal/thermal_zone0/temp')) {
    $raw = @file_get_contents('/sys/class/thermal/thermal_zone0/temp');
    if ($raw && is_numeric(trim($raw))) {
        $temp = sprintf("%.1f°C", trim($raw) / 1000);
    }
}
if (empty($temp)) {
    $temp = @exec('vcgencmd measure_temp 2>/dev/null | cut -d\'=\' -f2');
}
if (empty($temp)) {
    $temp = "N/A";
}

$ip_addr = @exec('hostname -I | cut -d\' \' -f1');
if (empty($ip_addr)) {
    $ip_addr = "N/A";
}

// Wurde als reiner Test aufgerufen (via Test-Button)
$isTest = isset($argv[1]) && $argv[1] === 'test';
$isBoot = isset($argv[1]) && $argv[1] === 'boot';

if ($isTest) {
    $msg = "✅ *Testnachricht gesendet:*\n";
} elseif ($isBoot) {
    // --- SPAM-SCHUTZ: Erst beim echten System-Boot benachrichtigen ---
    $uptime_file = '/var/www/html/data/last_system_boot_uptime.txt';
    $current_uptimes = @file_get_contents('/proc/uptime');
    $current_seconds = $current_uptimes ? (float)explode(' ', $current_uptimes)[0] : 0;
    
    if (file_exists($uptime_file)) {
        $last_seconds = (float)@file_get_contents($uptime_file);
        // Wenn die aktuelle Uptime GRÖSSER ist als die letzte gespeicherte,
        // dann lief das System zwischendurch weiter -> Nur Container-Neustart!
        // Wir erlauben eine kleine Differenz von 10s für Dateizugriffe.
        if ($current_seconds > $last_seconds) {
            // Schreibe die Uptime trotzdem fort, um den Zähler aktuell zu halten
            @file_put_contents($uptime_file, $current_seconds);
            exit("Spam-Schutz: Container-Neustart erkannt (System läuft seit $current_seconds Sek.). Keine Nachricht gesendet.\n");
        }
    }
    // Bei echtem Reboot (current < last) oder Erststart: Nachricht senden und Uptime speichern.
    @file_put_contents($uptime_file, $current_seconds);
    $msg = "✅ *System gestartet:*\n";
} else {
    $msg = "✅ *Täglicher Statusbericht:*\n";
}


// HA-Status abrufen
$haFile = '/var/www/html/ramdisk/ha_status.json';
$haInfo = "";
if (file_exists($haFile)) {
    $haData = @json_decode(file_get_contents($haFile), true);
    if ($haData) {
        $haMode = strtolower($haData['mode'] ?? 'off');
        $haState = strtolower($haData['state'] ?? 'unknown');
        $peerOnline = $haData['peer_online'] ?? false;
        
        if ($haMode === 'master') {
            $haInfo = "🛡 *HA-Cluster:* Master aktiv\n";
            $haInfo .= $peerOnline ? "✅ Backup-System bereit\n" : "⚠️ Backup-System OFFLINE\n";
        } elseif ($haMode === 'slave' && $haState === 'failover') {
            $haInfo = "🚨 *HA-Cluster:* SLAVE AKTIV (FAILOVER)\n";
            $haInfo .= "⚠️ Master-System OFFLINE!\n";
        }
    }
}

$msg .= "ℹ️ *" . $deviceName . " Online*\n\n";
$msg .= "📍 *IP:* " . $ip_addr . "\n";
$msg .= "⏱ *Laufzeit:* " . $uptime . "\n";
$msg .= "🌡 *Temp:* " . $temp . "\n";
if ($haInfo) {
    $msg .= "\n" . $haInfo;
}


$url = "https://api.telegram.org/bot" . $token . "/sendMessage";

$chat_ids = array_map('trim', explode(',', $chat_id));
foreach ($chat_ids as $cid) {
    if (empty($cid)) continue;
    $postData = ['chat_id' => $cid, 'text' => $msg, 'parse_mode' => 'Markdown'];
    $ch = curl_init();
    curl_setopt($ch, CURLOPT_URL, $url);
    curl_setopt($ch, CURLOPT_POST, 1);
    curl_setopt($ch, CURLOPT_POSTFIELDS, http_build_query($postData));
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($ch, CURLOPT_TIMEOUT, 10);
    curl_exec($ch);
    curl_close($ch);
}
?>
