<?php
require_once __DIR__ . '/helpers.php';
if (PHP_SAPI !== 'cli') {
    requireWebAuth(true);
}

$paths = getInstallPaths();
$confRes = loadE3dcConfig();
$config = $confRes['config'] ?? [];

function telegramConfigTruthy($value) {
    if (is_bool($value)) {
        return $value;
    }
    if (is_int($value) || is_float($value)) {
        return (float)$value !== 0.0;
    }
    $text = strtolower(trim((string)$value));
    return in_array($text, ['1', 'true', 'yes', 'on', 'ja'], true);
}

$stats_enabled = $config['telegram_stats_enable'] ?? '0';
if (!telegramConfigTruthy($stats_enabled)) {
    exit(0);
}

$token = $config['telegram_token'] ?? '';
$chat_id = $config['telegram_chat_id'] ?? '';
$deviceName = $config['telegram_device_name'] ?? 'E3DC-Control';

function telegramDailyLog($message) {
    $line = '[daily_telegram] ' . $message;
    error_log($line);
    echo $line . PHP_EOL;
}

function sendDailyTelegramMessage($token, $chat_id, $msg) {
    if (!function_exists('curl_init')) {
        telegramDailyLog('cURL ist nicht verfügbar; Tagesstatistik kann nicht gesendet werden.');
        return false;
    }
    $url = "https://api.telegram.org/bot" . $token . "/sendMessage";
    $chat_ids = array_values(array_filter(array_map('trim', explode(',', $chat_id)), 'strlen'));
    if (empty($chat_ids)) {
        telegramDailyLog('Keine Telegram-Chat-ID konfiguriert.');
        return false;
    }

    $anyOk = false;
    foreach ($chat_ids as $idx => $cid) {
        $attempts = ['Markdown', null];
        foreach ($attempts as $parseMode) {
            $postData = ['chat_id' => $cid, 'text' => $msg];
            if ($parseMode !== null) {
                $postData['parse_mode'] = $parseMode;
            }
            $ch = curl_init();
            curl_setopt($ch, CURLOPT_URL, $url);
            curl_setopt($ch, CURLOPT_POST, 1);
            curl_setopt($ch, CURLOPT_POSTFIELDS, http_build_query($postData));
            curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
            curl_setopt($ch, CURLOPT_TIMEOUT, 10);
            $response = curl_exec($ch);
            $curlError = curl_error($ch);
            $httpCode = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
            curl_close($ch);

            $decoded = is_string($response) ? json_decode($response, true) : null;
            $telegramOk = is_array($decoded) && !empty($decoded['ok']);
            if ($response !== false && $httpCode >= 200 && $httpCode < 300 && $telegramOk) {
                telegramDailyLog('Tagesstatistik gesendet' . ($parseMode === null ? ' (Plaintext-Fallback).' : '.'));
                $anyOk = true;
                break;
            }

            $desc = is_array($decoded) ? (string)($decoded['description'] ?? '') : '';
            $detail = $curlError ?: ($desc ?: ('HTTP ' . $httpCode));
            telegramDailyLog('Senden fehlgeschlagen für Chat #' . ($idx + 1) . ($parseMode === null ? ' ohne Markdown' : ' mit Markdown') . ': ' . $detail);
            if ($parseMode === null) {
                break;
            }
        }
    }
    return $anyOk;
}

if (empty($token) || empty($chat_id)) {
    telegramDailyLog('Telegram-Token oder Chat-ID fehlt; Tagesstatistik kann nicht gesendet werden.');
    exit(1);
}

// SLAVE-BUG FIX: Nichts senden, wenn wir im Standby-Modus sind!
$ha_mode = strtolower($config['ha_mode'] ?? 'off');
if ($ha_mode === 'slave') {
    $haFile = '/var/www/html/ramdisk/ha_status.json';
    if (file_exists($haFile)) {
        $haData = @json_decode(file_get_contents($haFile), true);
        if ($haData && isset($haData['state']) && $haData['state'] !== 'failover') {
            exit(0); // Lautlos beenden
        }
    }
}

$dbPath = '/var/www/html/data/e3dc_stats.db';
if (!file_exists($dbPath)) {
    telegramDailyLog('Langzeit-Datenbank fehlt: ' . $dbPath);
    exit(1);
}

try {
    $db = new PDO('sqlite:' . $dbPath);
    $db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
    try { $db->exec("ALTER TABLE daily_stats ADD COLUMN climate_consumption REAL DEFAULT 0"); } catch (Exception $e) { }
} catch (Throwable $e) {
    telegramDailyLog('Langzeit-Datenbank konnte nicht geöffnet werden: ' . $e->getMessage());
    exit(1);
}

$targetDate = date('Y-m-d', strtotime('-1 days'));
$stmt = $db->prepare("SELECT * FROM daily_stats WHERE date = ?");
$stmt->execute([$targetDate]);
$row = $stmt->fetch(PDO::FETCH_ASSOC);

// Test-Modus für frisch installierte Systeme (Heute nehmen, falls Gestern fehlt)
if (!$row) {
    $targetDate = date('Y-m-d');
    $stmt->execute([$targetDate]);
    $row = $stmt->fetch(PDO::FETCH_ASSOC);
    if (!$row) {
        $msg = "ℹ️ *$deviceName:*\nEs sind noch keine Daten in der Langzeit-Datenbank vorhanden.";
        if (!sendDailyTelegramMessage($token, $chat_id, $msg)) {
            exit(1);
        }
        exit(0);
    }
    $dateFormatted = date('d.m.Y') . " (Heute)";
} else {
    $dateFormatted = date('d.m.Y', strtotime("-1 days"));
}

$showWp = isset($config['wp']) && (strtolower($config['wp']) === 'true' || $config['wp'] == '1') || (isset($config['luxtronik']) && $config['luxtronik'] == '1');
$climateConsumption = (float)($row['climate_consumption'] ?? 0);
$showClimate = telegramConfigTruthy($config['climate_enable'] ?? '0') || $climateConsumption > 0.05;

$total_cons = $row['home_consumption'] + $row['wb_consumption'] + $row['wp_consumption'] + $climateConsumption;

$msg = "📊 *$deviceName Tagesstatistik ($dateFormatted)*\n\n";
$msg .= "☀️ *PV Ertrag:* " . number_format($row['pv_yield'], 2, ',', '.') . " kWh\n";
$msg .= "🔌 *Netzbezug:* " . number_format($row['grid_in'], 2, ',', '.') . " kWh\n";
$msg .= "🔋 *Batterie (Out):* " . number_format($row['bat_out'], 2, ',', '.') . " kWh\n\n";
$msg .= "🏠 *Hausverbrauch:* " . number_format($row['home_consumption'], 2, ',', '.') . " kWh\n";

if ($showWp) {
    $msg .= "🚗 *Wallbox:* " . number_format($row['wb_consumption'], 2, ',', '.') . " kWh\n";
    $msg .= "♨️ *Wärmepumpe:* " . number_format($row['wp_consumption'], 2, ',', '.') . " kWh\n";
} else {
    $msg .= "🚗 *Wallbox:* " . number_format($row['wb_consumption'], 2, ',', '.') . " kWh\n";
}
if ($showClimate) {
    $msg .= "❄️ *Klima:* " . number_format($climateConsumption, 2, ',', '.') . " kWh\n";
}
$msg .= "\n";

$msg .= "📈 *Tages-Autarkie:* " . number_format($row['autarky'], 1, ',', '.') . "%\n";
$msg .= "♻️ *Eigenverbrauch:* " . number_format($row['self_con'], 1, ',', '.') . "%\n";
$msg .= "⚡ *Gesamtverbrauch:* " . number_format($total_cons, 2, ',', '.') . " kWh\n\n";

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

$msg .= "🍓 *" . $deviceName . " Status*\n";
$msg .= "⏱ *Laufzeit:* " . $uptime . "\n";
$msg .= "🌡 *Temp:* " . $temp . "\n";
if ($haInfo) {
    $msg .= "\n" . $haInfo;
}

if (!sendDailyTelegramMessage($token, $chat_id, $msg)) {
    exit(1);
}
?>
