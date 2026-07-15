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

$weekly_enabled = $config['telegram_weekly_enable'] ?? '0';
if (!telegramConfigTruthy($weekly_enabled)) {
    exit(0);
}

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
            exit(0); // Lautlos beenden
        }
    }
}

$dbPath = '/var/www/html/data/e3dc_stats.db';
if (!file_exists($dbPath)) {
    exit(0);
}

$db = new PDO('sqlite:' . $dbPath);
$db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
try { $db->exec("ALTER TABLE daily_stats ADD COLUMN climate_consumption REAL DEFAULT 0"); } catch (Exception $e) { }

$startDateStr = date('Y-m-d', strtotime('-6 days'));
$endDateStr = date('Y-m-d');

$query = "SELECT 
            SUM(pv_yield) as pv_yield,
            SUM(home_consumption) as home,
            SUM(grid_in) as grid_in,
            SUM(grid_out) as grid_out,
	            SUM(bat_out) as bat_out,
	            SUM(wb_consumption) as wb,
	            SUM(wp_consumption) as wp,
	            SUM(COALESCE(climate_consumption, 0)) as climate
		          FROM daily_stats
          WHERE date >= ? AND date <= ?";
          
$stmt = $db->prepare($query);
$stmt->execute([$startDateStr, $endDateStr]);
$row = $stmt->fetch(PDO::FETCH_ASSOC);

if (!$row || $row['pv_yield'] === null) {
    exit(0);
}

$climateConsumption = (float)($row['climate'] ?? 0);
$total_cons = $row['home'] + $row['wb'] + $row['wp'] + $climateConsumption;
$autarky = $total_cons > 0 ? max(0, min(100, (($total_cons - $row['grid_in']) / $total_cons) * 100)) : 100;
$selfcon = $row['pv_yield'] > 0 ? max(0, min(100, (($row['pv_yield'] - $row['grid_out']) / $row['pv_yield']) * 100)) : 100;

$showWp = isset($config['wp']) && (strtolower($config['wp']) === 'true' || $config['wp'] == '1') || (isset($config['luxtronik']) && $config['luxtronik'] == '1');
$showClimate = telegramConfigTruthy($config['climate_enable'] ?? '0') || $climateConsumption > 0.05;
$startDateFmt = date('d.m.', strtotime('-6 days')); 
$endDateFmt = date('d.m.Y');

$msg = "📅 *$deviceName Wochenrückblick ($startDateFmt - $endDateFmt)*\n\n";
$msg .= "☀️ *PV Ertrag:* " . number_format($row['pv_yield'], 2, ',', '.') . " kWh\n";
$msg .= "🔌 *Netzbezug:* " . number_format($row['grid_in'], 2, ',', '.') . " kWh\n";
$msg .= "🔋 *Batterie (Out):* " . number_format($row['bat_out'], 2, ',', '.') . " kWh\n\n";
$msg .= "🏠 *Hausverbrauch:* " . number_format($row['home'], 2, ',', '.') . " kWh\n";

if ($showWp) {
    $msg .= "🚗 *Wallbox:* " . number_format($row['wb'], 2, ',', '.') . " kWh\n";
    $msg .= "♨️ *Wärmepumpe:* " . number_format($row['wp'], 2, ',', '.') . " kWh\n";
} else {
    $msg .= "🚗 *Wallbox:* " . number_format($row['wb'], 2, ',', '.') . " kWh\n";
}
if ($showClimate) {
    $msg .= "❄️ *Klima:* " . number_format($climateConsumption, 2, ',', '.') . " kWh\n";
}
$msg .= "\n";

$msg .= "📈 *Wochen-Autarkie:* " . number_format($autarky, 1, ',', '.') . "%\n";
$msg .= "♻️ *Eigenverbrauch:* " . number_format($selfcon, 1, ',', '.') . "%\n";
$msg .= "⚡ *Gesamtverbrauch:* " . number_format($total_cons, 2, ',', '.') . " kWh\n";

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

if ($haInfo) {
    $msg .= "\n" . $haInfo;
}


$url = "https://api.telegram.org/bot" . $token . "/sendMessage";

$chat_ids = array_map('trim', explode(',', $chat_id));
foreach ($chat_ids as $cid) {
    if (empty($cid)) continue;
    $postData = ['chat_id' => $cid, 'text' => $msg, 'parse_mode' => 'Markdown'];
    $ch = curl_init(); curl_setopt($ch, CURLOPT_URL, $url); curl_setopt($ch, CURLOPT_POST, 1); curl_setopt($ch, CURLOPT_POSTFIELDS, http_build_query($postData)); curl_setopt($ch, CURLOPT_RETURNTRANSFER, true); curl_setopt($ch, CURLOPT_TIMEOUT, 10); curl_exec($ch); curl_close($ch);
}
?>
