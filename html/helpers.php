<?php
/**
 * helpers.php - Zentrale Utility-Funktionen für E3DC-Control Web-Interface
 */

if (PHP_SAPI !== 'cli' && session_status() === PHP_SESSION_NONE) {
    session_start();
}

date_default_timezone_set('Europe/Berlin');

if (!defined('WEB_AUTH_FAILURE_LIMIT')) {
    define('WEB_AUTH_FAILURE_LIMIT', 5);
}
if (!defined('WEB_AUTH_FAILURE_WINDOW_S')) {
    define('WEB_AUTH_FAILURE_WINDOW_S', 600);
}

/**
 * Erkennt den Containerkontext unabhängig von einem einzelnen Docker-Marker.
 *
 * Offizielle Images setzen E3DC_CONTAINER_MODE. /.dockerenv erhält die
 * Kompatibilität zu älteren Docker-Installationen, ohne dafür Zugriff auf den
 * Docker-Socket zu benötigen.
 */
function e3dcIsDockerEnvironment() {
    if (is_file('/.dockerenv')) return true;

    $explicit = strtolower(trim((string)(getenv('E3DC_CONTAINER_MODE') ?: '')));
    return in_array($explicit, ['1', 'true', 'yes', 'docker'], true);
}

/**
 * Sendet HTTP-Header, die das Caching der PHP-Seite durch Browser und Proxies strikt verbieten.
 */
function sendNoCacheHeaders() {
    header('Cache-Control: no-store, no-cache, must-revalidate, max-age=0');
    header('Cache-Control: post-check=0, pre-check=0', false);
    header('Pragma: no-cache');
    header('Expires: 0');
}

/**
 * Erzeugt einen Cache-Busting-Pfad für statische Dateien (JS/CSS) basierend auf dem Änderungsdatum.
 * Sucht automatisch nach einer .min Version, falls vorhanden.
 */
function getAssetUrl($filePath) {
    $fullPath = __DIR__ . '/' . ltrim($filePath, '/');

    // Automatisch nach .min-Version suchen (z.B. solar.js -> solar.min.js)
    if (strpos($filePath, '.js') !== false && strpos($filePath, '.min.js') === false) {
        $minFilePath = str_replace('.js', '.min.js', $filePath);
        $fullMinPath = __DIR__ . '/' . ltrim($minFilePath, '/');
        if (file_exists($fullMinPath) && file_exists($fullPath) && filemtime($fullMinPath) >= filemtime($fullPath)) {
            $filePath = $minFilePath;
            $fullPath = $fullMinPath;
        }
    }

    if (file_exists($fullPath)) {
        return $filePath . '?v=' . filemtime($fullPath);
    }
    return $filePath;
}

/**
 * Berechnet das Alter nur aus einem echten, plausiblen JSON-Zeitstempel.
 */
function e3dcLiveMeasurementAgeSeconds($timestamp, $nowTs) {
    if ((!is_int($timestamp) && !is_float($timestamp))
        || (!is_int($nowTs) && !is_float($nowTs))
    ) {
        return null;
    }
    $reportedAt = (float)$timestamp;
    $now = (float)$nowTs;
    if (!is_finite($reportedAt)
        || !is_finite($now)
        || $reportedAt <= 0.0
        || ($reportedAt - $now) > 5.0
    ) {
        return null;
    }
    return round(max(0.0, $now - $reportedAt), 1);
}

/**
 * Messwertverträge akzeptieren ausschließlich explizite JSON-Booleans.
 */
function e3dcLiveMeasurementConfirmed($reportedValid, $online) {
    return $reportedValid === true && $online === true;
}

/**
 * Validiert eine Netzfrequenz-Messung als strikt typisierten, frischen Wert.
 *
 * Ein numerischer String ist kein Messwertvertrag. Der Aufrufer muss die
 * Messung ausdrücklich bestätigen und ihr Alter aus der tatsächlichen
 * Publikationszeit ableiten.
 */
function e3dcLiveFrequencyProjection($value, $reportedValid, $source, $ageS, $maxAgeS) {
    $projection = [
        'frequency_hz' => null,
        'valid' => false,
        'source' => (string)($source ?: 'unavailable'),
        'age_s' => null,
    ];
    if (!is_int($value) && !is_float($value)) return $projection;
    if (!is_int($ageS) && !is_float($ageS)) return $projection;

    $frequency = (float)$value;
    $age = (float)$ageS;
    $maxAge = max(0.0, (float)$maxAgeS);
    if ($reportedValid !== true
        || !is_finite($frequency)
        || $frequency < 45.0
        || $frequency > 55.0
        || !is_finite($age)
        || $age < 0.0
        || $age > $maxAge
    ) {
        return $projection;
    }

    $projection['frequency_hz'] = round($frequency, 3);
    $projection['valid'] = true;
    $projection['age_s'] = round($age, 1);
    return $projection;
}

/**
 * Bevorzugt eine gültige Primärmessung, sonst eine gültige Ersatzmessung.
 */
function e3dcSelectLiveFrequencyProjection($primary, $fallback) {
    if (is_array($primary) && ($primary['valid'] ?? false) === true) {
        return $primary;
    }
    if (is_array($fallback) && ($fallback['valid'] ?? false) === true) {
        return $fallback;
    }
    return [
        'frequency_hz' => null,
        'valid' => false,
        'source' => 'unavailable',
        'age_s' => null,
    ];
}

// ==================== AUTHENTHIFIZIERUNG ====================

function e3dcWebAuthHashEquals($expected, $actual) {
    if (function_exists('hash_equals')) {
        return hash_equals((string)$expected, (string)$actual);
    }
    return (string)$expected === (string)$actual;
}

function e3dcWebAuthFailureFile() {
    $ramdisk = '/var/www/html/ramdisk';
    if (is_dir($ramdisk) && is_writable($ramdisk)) {
        return $ramdisk . '/web_auth_failures.json';
    }
    return sys_get_temp_dir() . '/e3dc_web_auth_failures.json';
}

function e3dcWebAuthClientKey() {
    $remoteAddr = $_SERVER['REMOTE_ADDR'] ?? 'unknown';
    $userAgent = $_SERVER['HTTP_USER_AGENT'] ?? '';
    return hash('sha256', $remoteAddr . '|' . $userAgent);
}

function e3dcWebAuthReadFailures() {
    $file = e3dcWebAuthFailureFile();
    if (!is_readable($file)) return [];
    $raw = @file_get_contents($file);
    $data = json_decode((string)$raw, true);
    return is_array($data) ? $data : [];
}

function e3dcWebAuthWriteFailures($data) {
    $file = e3dcWebAuthFailureFile();
    $dir = dirname($file);
    if (!is_dir($dir) || !is_writable($dir)) return;
    $tmp = $file . '.tmp';
    @file_put_contents($tmp, json_encode($data, JSON_UNESCAPED_SLASHES));
    @rename($tmp, $file);
    @chmod($file, 0664);
}

function e3dcWebAuthPruneFailures($data, $now = null) {
    $now = $now ?? time();
    $pruned = [];
    foreach ($data as $key => $record) {
        if (!is_array($record)) continue;
        $lastTs = (int)($record['last_ts'] ?? 0);
        $lockedUntil = (int)($record['locked_until'] ?? 0);
        if ($lockedUntil > $now || ($now - $lastTs) <= WEB_AUTH_FAILURE_WINDOW_S) {
            $pruned[$key] = $record;
        }
    }
    return $pruned;
}

function e3dcWebAuthLockRemaining($clientKey = null) {
    $clientKey = $clientKey ?? e3dcWebAuthClientKey();
    $now = time();
    $data = e3dcWebAuthPruneFailures(e3dcWebAuthReadFailures(), $now);
    $record = $data[$clientKey] ?? [];
    $lockedUntil = (int)($record['locked_until'] ?? 0);
    return max(0, $lockedUntil - $now);
}

function e3dcWebAuthRecordFailure($clientKey = null) {
    $clientKey = $clientKey ?? e3dcWebAuthClientKey();
    $now = time();
    $data = e3dcWebAuthPruneFailures(e3dcWebAuthReadFailures(), $now);
    $record = $data[$clientKey] ?? ['count' => 0, 'first_ts' => $now, 'last_ts' => 0, 'locked_until' => 0];
    if (($now - (int)($record['first_ts'] ?? 0)) > WEB_AUTH_FAILURE_WINDOW_S) {
        $record = ['count' => 0, 'first_ts' => $now, 'last_ts' => 0, 'locked_until' => 0];
    }
    $record['count'] = (int)($record['count'] ?? 0) + 1;
    $record['last_ts'] = $now;
    if ($record['count'] >= WEB_AUTH_FAILURE_LIMIT) {
        $record['locked_until'] = $now + WEB_AUTH_FAILURE_WINDOW_S;
    }
    $data[$clientKey] = $record;
    e3dcWebAuthWriteFailures($data);
    return max(0, (int)($record['locked_until'] ?? 0) - $now);
}

function e3dcWebAuthClearFailures($clientKey = null) {
    $clientKey = $clientKey ?? e3dcWebAuthClientKey();
    $data = e3dcWebAuthReadFailures();
    if (isset($data[$clientKey])) {
        unset($data[$clientKey]);
        e3dcWebAuthWriteFailures($data);
    }
}

/**
 * Sperrt Browserzugriffe fremder Origins fail-closed.
 *
 * Die Web-PIN ist weder eine Origin-Allowlist noch ein eigenständiger
 * Capability-Token. Insbesondere darf eine leere optionale Web-PIN niemals
 * dazu führen, dass eine beliebige Website Dashboard, Sitzung oder CSRF-Token
 * lesen kann. Eine künftige externe Browser-App braucht einen getrennten,
 * explizit konfigurierten Origin- und Tokenvertrag.
 */
function handleCORSAndExternalAuth() {
    $origin = $_SERVER['HTTP_ORIGIN'] ?? '';
    if (!empty($origin)) {
        header('Vary: Origin');
    }
    if (($_SERVER['REQUEST_METHOD'] ?? '') === 'OPTIONS') {
        http_response_code(403);
        header('Content-Type: application/json; charset=utf-8');
        echo json_encode([
            'success' => false,
            'error' => 'Cross-Origin-Browserzugriff ist nicht freigegeben.',
        ]);
        exit;
    }
}

// CORS & Preflight-Handling sofort beim Laden ausführen
handleCORSAndExternalAuth();

handleDiagnoseAck();

function isWebAuthenticated() {
    if (PHP_SAPI === 'cli') {
        return true;
    }
    $conf = loadE3dcConfig();

    if (
        !is_array($conf)
        || !empty($conf['error'])
        || !isset($conf['config'])
        || !is_array($conf['config'])
    ) {
        return false;
    }

    // 1. Session-basierte Anmeldung (normaler Browser)
    if (isset($_SESSION['web_authenticated']) && $_SESSION['web_authenticated'] === true) return true;

    $pin = $conf['config']['web_pin'] ?? '';
    if ($pin === '') return true; // Kein PIN gesetzt -> Jeder ist authentifiziert

    // 2. Token-basierte Anmeldung (für Widgets und externe Apps)
    $authHeader = $_SERVER['HTTP_AUTHORIZATION'] ?? $_SERVER['REDIRECT_HTTP_AUTHORIZATION'] ?? '';
    $token = '';
    if (preg_match('/Bearer\s+(.*)$/i', $authHeader, $matches)) {
        $token = trim($matches[1]);
    } elseif (isset($_SERVER['HTTP_X_API_PIN'])) {
        $token = trim($_SERVER['HTTP_X_API_PIN']);
    }

    if ($token !== '' && e3dcWebAuthHashEquals($pin, $token)) {
        return true;
    }

    return false;
}

function requireWebAuth($isAjax = true) {
    if (!isWebAuthenticated()) {
        if ($isAjax || (isset($_SERVER['HTTP_X_REQUESTED_WITH']) && strtolower($_SERVER['HTTP_X_REQUESTED_WITH']) == 'xmlhttprequest')) {
            http_response_code(403);
            echo json_encode(['success' => false, 'error' => 'PIN erforderlich', 'message' => 'Bitte zuerst mit PIN anmelden.']);
            exit;
        } else {
            header('Location: ' . getContextPageUrl('lock'));
            exit;
        }
    }
}

function e3dcCsrfToken() {
    if (empty($_SESSION['csrf_token']) || !is_string($_SESSION['csrf_token'])) {
        $_SESSION['csrf_token'] = bin2hex(random_bytes(32));
    }
    return $_SESSION['csrf_token'];
}

function e3dcCsrfInput() {
    return '<input type="hidden" name="csrf_token" value="' . htmlspecialchars(e3dcCsrfToken(), ENT_QUOTES, 'UTF-8') . '">';
}

function e3dcRequireCsrfToken($isAjax = true) {
    $expected = e3dcCsrfToken();
    $sent = $_POST['csrf_token'] ?? ($_SERVER['HTTP_X_CSRF_TOKEN'] ?? '');
    if (!is_string($sent)) {
        $sent = '';
    }
    if ($sent === '' || !e3dcWebAuthHashEquals($expected, $sent)) {
        http_response_code(403);
        if ($isAjax || (isset($_SERVER['HTTP_X_REQUESTED_WITH']) && strtolower($_SERVER['HTTP_X_REQUESTED_WITH']) === 'xmlhttprequest')) {
            header('Content-Type: application/json; charset=utf-8');
            echo json_encode(['success' => false, 'ok' => false, 'error' => 'CSRF-Token ungültig', 'msg' => 'CSRF-Token ungültig. Bitte Seite neu laden.', 'message' => 'Bitte Seite neu laden.']);
        } else {
            echo 'Fehler: CSRF-Token ungültig. Bitte Seite neu laden.';
        }
        exit;
    }
}

function e3dcRequirePostMutation($isAjax = true) {
    if (strtoupper((string)($_SERVER['REQUEST_METHOD'] ?? 'GET')) !== 'POST') {
        http_response_code(405);
        header('Allow: POST');
        if ($isAjax || (isset($_SERVER['HTTP_X_REQUESTED_WITH']) && strtolower($_SERVER['HTTP_X_REQUESTED_WITH']) === 'xmlhttprequest')) {
            header('Content-Type: application/json; charset=utf-8');
            echo json_encode([
                'success' => false,
                'ok' => false,
                'error' => 'Methode nicht erlaubt',
                'message' => 'Diese Aktion ist ausschließlich per POST erlaubt.',
            ]);
        } else {
            echo 'Fehler: Diese Aktion ist ausschließlich per POST erlaubt.';
        }
        exit;
    }
    requireWebAuth($isAjax);
    e3dcRequireCsrfToken($isAjax);
}

function handleWebLogin() {
    if (isset($_POST['action']) && $_POST['action'] === 'web_login') {
        e3dcRequireCsrfToken(false);
        $conf = loadE3dcConfig();
        $pin = $conf['config']['web_pin'] ?? '';
        $clientKey = e3dcWebAuthClientKey();
        $lockRemaining = e3dcWebAuthLockRemaining($clientKey);
        if ($lockRemaining > 0) {
            $_SESSION['login_error'] = true;
            $_SESSION['login_error_message'] = 'Zu viele falsche PIN-Versuche. Bitte warte ' . ceil($lockRemaining / 60) . ' Minuten.';
        } elseif ($pin !== '' && isset($_POST['pin']) && e3dcWebAuthHashEquals($pin, $_POST['pin'])) {
            session_regenerate_id(true);
            $_SESSION['web_authenticated'] = true;
            unset($_SESSION['login_error']);
            unset($_SESSION['login_error_message']);
            e3dcWebAuthClearFailures($clientKey);
        } else {
            $lockRemaining = e3dcWebAuthRecordFailure($clientKey);
            $_SESSION['login_error'] = true;
            if ($lockRemaining > 0) {
                $_SESSION['login_error_message'] = 'Zu viele falsche PIN-Versuche. Bitte warte ' . ceil($lockRemaining / 60) . ' Minuten.';
            } else {
                unset($_SESSION['login_error_message']);
            }
        }
        header('Location: ' . ($_SERVER['HTTP_REFERER'] ?? getContextPageUrl('dashboard')));
        exit;
    }
    if (isset($_POST['action']) && $_POST['action'] === 'web_logout') {
        e3dcRequirePostMutation(false);
        unset($_SESSION['web_authenticated']);
        session_regenerate_id(true);
        header('Location: ' . getContextPageUrl('dashboard'));
        exit;
    }
}

function handleDiagnoseAck() {
    if (isset($_GET['action']) && $_GET['action'] === 'ack_diagnose') {
        e3dcRequirePostMutation(true);
        $ackState = ['time' => time(), 'sizes' => []];
        $logFiles = [
            'energy_manager' => '/var/www/html/logs/energy_manager.log',
            'ha_manager'     => '/var/www/html/logs/ha_manager.log',
            'watchdog'       => '/var/www/html/logs/piguard.log',
            'notifier'       => '/var/www/html/logs/notification_manager.log',
            'bluelink'       => '/var/www/html/logs/bluelink_client.log',
            'mqtt_hub'       => '/var/www/html/logs/e3dc_mqtt_hub.log',
            'websocket'      => '/var/www/html/logs/e3dc_websocket.log',
            'update'         => '/var/www/html/logs/update.log',
            'self_update'    => '/var/log/e3dc-control/web-update.log'
        ];
        foreach ($logFiles as $key => $file) {
            if (file_exists($file)) $ackState['sizes'][$key] = filesize($file);
        }
        file_put_contents('/var/www/html/ramdisk/diagnose_ack.json', json_encode($ackState));
        @chmod('/var/www/html/ramdisk/diagnose_ack.json', 0664);
        header('Content-Type: application/json');
        echo json_encode(['success' => true]);
        exit;
    }
}

// ==================== ERROR HANDLING ====================

/**
 * Liefert das Log des HA Managers (AJAX).
 */
function handleHAManagerLog() {
    if (isset($_GET['action']) && $_GET['action'] === 'get_ha_log') {
        requireWebAuth(true);
        header('Content-Type: text/plain; charset=utf-8');
        $logFile = '/var/www/html/logs/ha_manager.log';
        if (file_exists($logFile) && is_readable($logFile)) {
            $last_lines = e3dcReadTextTailLines($logFile, 150, 1024 * 1024);
            echo "--- Letzte 150 Einträge aus ha_manager.log ---\n\n";
            echo implode("\n", $last_lines);
        } else {
            echo "Log-Datei nicht gefunden oder nicht lesbar unter: " . htmlspecialchars($logFile);
        }
        exit;
    }
}

/**
 * Setzt das Flag für einen Force-Refresh des Auto-SoC
 */
function handleForceSocUpdate() {
    if (isset($_GET['action']) && $_GET['action'] === 'force_soc') {
        e3dcRequirePostMutation(true);
        header('Content-Type: application/json');
        $flagFile = '/var/www/html/ramdisk/force_bluelink.flag';

        // Nur aufwecken wenn Bluelink Token konfiguriert ist
        $conf = loadE3dcConfig();
        $hasBluelink = !empty($conf['config']['bluelink_refresh_token']);
        // Auch V4 JSON prüfen (Token könnte dort gespeichert sein)
        if (!$hasBluelink) {
            $v4 = @json_decode(@file_get_contents('/var/www/html/data/e3dc_v4.json'), true);
            $hasBluelink = !empty($v4['bluelink_refresh_token']);
        }

        if (!$hasBluelink) {
            echo json_encode(['success' => false, 'message' => 'Kein Bluelink Token konfiguriert']);
            exit;
        }

        @touch($flagFile);
        @chmod($flagFile, 0666);
        echo json_encode(['success' => true]);
        exit;
    }
}

/**
 * Testet einen E3DC PM-Index per RSCP und gibt Plausibilität zurück (AJAX).
 */
function handleTestPmIndex() {
    if (isset($_GET['action']) && $_GET['action'] === 'test_pm_index') {
        requireWebAuth(true);
        header('Content-Type: application/json; charset=utf-8');
        $index = isset($_GET['index']) ? (int)$_GET['index'] : (isset($_POST['index']) ? (int)$_POST['index'] : 2);
        if ($index < 0 || $index > 7) {
            echo json_encode(['success' => false, 'error' => 'invalid_index', 'message' => 'Ungültiger PM-Index (erlaubt 0..7)']);
            exit;
        }
        $paths = function_exists('getInstallPaths') ? getInstallPaths() : [];
        $installPath = !empty($paths['valid']) ? rtrim($paths['install_path'], '/') : '';
        $script = $installPath . '/Installer/probe_pm.py';
        if (!file_exists($script)) {
            $script = '/var/www/html/Installer/probe_pm.py';
        }
        if (!file_exists($script)) {
            echo json_encode(['success' => false, 'error' => 'script_missing', 'message' => 'Test-Skript probe_pm.py nicht gefunden']);
            exit;
        }
        $python = file_exists('/opt/venv/bin/python3') ? '/opt/venv/bin/python3' : '/usr/bin/python3';
        $process = e3dcRunArgvProcess([$python, $script, '--index', (string)$index, '--json'], 5.0);
        if (!$process['success'] && empty($process['stdout'])) {
            echo json_encode([
                'success' => false,
                'error' => $process['timed_out'] ? 'timeout' : 'exec_error',
                'message' => $process['timed_out'] ? 'Zeitüberschreitung bei RSCP-Messung (5s)' : ('Ausführung fehlgeschlagen: ' . ($process['error'] ?: $process['stderr'])),
            ], JSON_UNESCAPED_UNICODE);
            exit;
        }
        $json = @json_decode((string)$process['stdout'], true);
        if (is_array($json)) {
            echo json_encode($json, JSON_UNESCAPED_UNICODE);
        } else {
            echo json_encode([
                'success' => false,
                'error' => 'json_parse_error',
                'message' => 'Ungültige Antwort von probe_pm.py: ' . trim((string)$process['stdout'] . ' ' . (string)$process['stderr']),
            ], JSON_UNESCAPED_UNICODE);
        }
        exit;
    }
}

/**
 * Liefert System-Logs für das Diagnose-Fenster (AJAX).
 */
function e3dcReadWallboxCommandGateEvents($limit = 12) {
    $limit = max(1, min(50, (int)$limit));
    $file = '/var/www/html/logs/wallbox_command_audit.log';
    if (!is_readable($file)) return [];
    $lines = @file($file, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    if (!$lines) return [];
    $events = [];
    for ($i = count($lines) - 1; $i >= 0 && count($events) < $limit; $i--) {
        $row = @json_decode($lines[$i], true);
        if (is_array($row)) $events[] = $row;
    }
    return $events;
}

function e3dcWallboxCommandGateDecisionLabel($decision) {
    $decision = strtolower(trim((string)$decision));
    if ($decision === 'allowed') return 'erlaubt';
    if ($decision === 'blocked') return 'blockiert';
    if ($decision === 'one_shot_allowed') return 'einmalig erlaubt';
    return $decision !== '' ? $decision : 'unbekannt';
}

function e3dcWallboxCommandGateDecisionColor($decision) {
    $decision = strtolower(trim((string)$decision));
    if ($decision === 'allowed') return 'success';
    if ($decision === 'blocked') return 'danger';
    if ($decision === 'one_shot_allowed') return 'warning';
    return 'secondary';
}

function e3dcWallboxCommandGatePayloadText($payload) {
    if ($payload === null || $payload === '') return '--';
    if (is_array($payload)) {
        $parts = [];
        foreach (['target_amp', 'amp', 'max_amp', 'phases', 'force_state', 'mode', 'is_heartbeat'] as $key) {
            if (array_key_exists($key, $payload)) $parts[] = $key . '=' . $payload[$key];
        }
        if (!empty($parts)) return implode(', ', $parts);
        $json = json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        return substr((string)$json, 0, 160);
    }
    return substr((string)$payload, 0, 160);
}

function e3dcWallboxCommandGateTimestampText($ts, $withRelative = true) {
    if ($ts === null || $ts === '' || !is_numeric($ts)) return '--';
    $timestamp = (int)floor((float)$ts);
    if ($timestamp <= 0) return '--';
    $absolute = date('d.m.Y H:i:s', $timestamp);
    if (!$withRelative) return $absolute;

    $eventDay = date('Y-m-d', $timestamp);
    $today = date('Y-m-d');
    $yesterday = date('Y-m-d', strtotime('-1 day'));
    if ($eventDay === $today) return 'Heute, ' . $absolute;
    if ($eventDay === $yesterday) return 'Gestern, ' . $absolute;
    return $absolute;
}

function e3dcFormatWallboxCommandGateLine($event) {
    if (!is_array($event)) return '';
    $ts = e3dcWallboxCommandGateTimestampText($event['ts'] ?? null, true);
    $wb = 'WB' . (int)($event['wb'] ?? 0);
    $decision = e3dcWallboxCommandGateDecisionLabel($event['decision'] ?? '');
    $driver = trim((string)($event['driver'] ?? ''));
    $action = trim((string)($event['action'] ?? ''));
    $reason = trim((string)($event['reason'] ?? ''));
    $payload = e3dcWallboxCommandGatePayloadText($event['payload'] ?? null);
    return $ts . ' | ' . $decision . ' | ' . $wb . ' | ' . $driver . ' | ' . $action . ' | ' . $reason . ' | ' . $payload;
}

function e3dcRenderWallboxCommandGateText($limit = 20) {
    $events = e3dcReadWallboxCommandGateEvents($limit);
    if (empty($events)) {
        return "Noch keine Wallbox-Command-Gate-Ereignisse vorhanden.\n\nSobald der Wallbox-Manager einen Schreibbefehl erlaubt oder blockiert, erscheint er hier.";
    }
    $last = $events[0];
    $out = "--- Wallbox Command-Gate Diagnose ---\n\n";
    $out .= "Zeitpunkt: " . e3dcWallboxCommandGateTimestampText($last['ts'] ?? null, true) . "\n";
    $out .= "Letzte Entscheidung: " . e3dcWallboxCommandGateDecisionLabel($last['decision'] ?? '') . "\n";
    $out .= "Wallbox: WB" . (int)($last['wb'] ?? 0) . "\n";
    $out .= "Treiber: " . ($last['driver'] ?? '--') . "\n";
    $out .= "Aktion: " . ($last['action'] ?? '--') . "\n";
    $out .= "Grund: " . ($last['reason'] ?? '--') . "\n";
    $out .= "Nutzlast: " . e3dcWallboxCommandGatePayloadText($last['payload'] ?? null) . "\n\n";
    $out .= "--- Letzte Ereignisse ---\n";
    foreach ($events as $event) {
        $out .= e3dcFormatWallboxCommandGateLine($event) . "\n";
    }
    return $out;
}

function e3dcDecisionHistoryFiles($prefix) {
    $files = array_merge(
        glob('/var/www/html/logs/' . $prefix . '*.jsonl') ?: [],
        glob('/var/www/html/logs/' . $prefix . '*.jsonl.gz') ?: []
    );
    rsort($files, SORT_STRING);
    return $files;
}

function e3dcLatestDecisionHistoryFile($prefix) {
    $files = e3dcDecisionHistoryFiles($prefix);
    return $files[0] ?? null;
}

function e3dcReadGzipJsonlTailRows($file, $limit = 20) {
    $limit = max(1, (int)$limit);
    if (!function_exists('gzopen') || !$file || !is_readable($file)) return [];
    $handle = @gzopen($file, 'rb');
    if (!$handle) return [];
    $lines = [];
    while (!gzeof($handle)) {
        $line = @gzgets($handle);
        if ($line === false) break;
        $line = trim($line);
        if ($line === '') continue;
        $lines[] = $line;
        if (count($lines) > $limit) {
            array_shift($lines);
        }
    }
    @gzclose($handle);
    $rows = [];
    foreach ($lines as $line) {
        $row = @json_decode($line, true);
        if (is_array($row)) $rows[] = $row;
    }
    return $rows;
}

function e3dcReadJsonlTailRows($file, $limit = 20, $maxBytes = 5242880) {
    $limit = max(1, (int)$limit);
    $maxBytes = max(65536, (int)$maxBytes);
    if (!$file || !is_readable($file)) return [];
    if (str_ends_with($file, '.gz')) {
        return e3dcReadGzipJsonlTailRows($file, $limit);
    }
    $size = @filesize($file);
    if ($size === false || $size <= 0) return [];
    $handle = @fopen($file, 'rb');
    if (!$handle) return [];
    $buffer = '';
    $pos = (int)$size;
    $chunkSize = 65536;
    while ($pos > 0 && substr_count($buffer, "\n") <= $limit && strlen($buffer) < $maxBytes) {
        $read = min($chunkSize, $pos);
        $pos -= $read;
        if (@fseek($handle, $pos) !== 0) break;
        $chunk = @fread($handle, $read);
        if ($chunk === false || $chunk === '') break;
        $buffer = $chunk . $buffer;
    }
    @fclose($handle);
    if ($buffer === '') return [];
    $lines = preg_split('/\r\n|\r|\n/', $buffer, -1, PREG_SPLIT_NO_EMPTY);
    if ($pos > 0 && count($lines) > 0) {
        array_shift($lines);
    }
    if (count($lines) > $limit) {
        $lines = array_slice($lines, -$limit);
    }
    $rows = [];
    foreach ($lines as $line) {
        $row = @json_decode($line, true);
        if (is_array($row)) $rows[] = $row;
    }
    return $rows;
}

function e3dcReadTextTailLines($file, $limit = 150, $maxBytes = 1048576, $skipEmpty = false) {
    $limit = max(1, (int)$limit);
    $maxBytes = max(65536, (int)$maxBytes);
    if (!$file || !is_readable($file)) return [];

    $size = @filesize($file);
    if ($size === false || $size <= 0) return [];

    $handle = @fopen($file, 'rb');
    if (!$handle) return [];

    $buffer = '';
    $pos = (int)$size;
    $chunkSize = 65536;
    while ($pos > 0 && substr_count($buffer, "\n") <= $limit && strlen($buffer) < $maxBytes) {
        $read = min($chunkSize, $pos);
        $pos -= $read;
        if (@fseek($handle, $pos) !== 0) break;
        $chunk = @fread($handle, $read);
        if ($chunk === false || $chunk === '') break;
        $buffer = $chunk . $buffer;
    }
    @fclose($handle);

    if ($buffer === '') return [];
    $flags = $skipEmpty ? PREG_SPLIT_NO_EMPTY : 0;
    $lines = preg_split('/\r\n|\r|\n/', $buffer, -1, $flags);
    if ($lines === false) return [];
    if (!$skipEmpty && end($lines) === '') array_pop($lines);
    if ($pos > 0 && count($lines) > 0) array_shift($lines);
    if (count($lines) > $limit) $lines = array_slice($lines, -$limit);
    $lines = array_map('e3dcEnsureUtf8Text', $lines);
    return $lines;
}

function e3dcReadLatestDecisionHistoryRows($prefix, $maxRows = 20) {
    $file = e3dcLatestDecisionHistoryFile($prefix);
    return e3dcReadJsonlTailRows($file, max(1, (int)$maxRows));
}

function e3dcWalkLatestDecisionHistoryRows($prefix, $callback) {
    $file = e3dcLatestDecisionHistoryFile($prefix);
    if (!$file || !is_readable($file) || !is_callable($callback)) return 0;
    $isGz = str_ends_with($file, '.gz');
    $handle = $isGz && function_exists('gzopen') ? @gzopen($file, 'rb') : @fopen($file, 'rb');
    if (!$handle) return 0;
    $count = 0;
    while ($isGz ? !gzeof($handle) : !feof($handle)) {
        $line = $isGz ? @gzgets($handle) : @fgets($handle);
        if ($line === false) break;
        $line = trim($line);
        if ($line === '') continue;
        $row = @json_decode($line, true);
        if (!is_array($row)) continue;
        $count++;
        $callback($row);
    }
    $isGz ? @gzclose($handle) : @fclose($handle);
    return $count;
}

function e3dcReadDecisionHistoryEventsByPrefix($prefix, $latestFile, $limit = 20) {
    $limit = max(1, min(80, (int)$limit));
    $events = [];
    $files = e3dcDecisionHistoryFiles($prefix);
    foreach ($files as $file) {
        $rows = e3dcReadJsonlTailRows($file, $limit - count($events));
        for ($i = count($rows) - 1; $i >= 0 && count($events) < $limit; $i--) {
            $events[] = $rows[$i];
        }
        if (count($events) >= $limit) break;
    }
    if (empty($events) && is_readable($latestFile)) {
        $row = @json_decode(@file_get_contents($latestFile), true);
        if (is_array($row)) $events[] = $row;
    }
    return $events;
}

function e3dcDecisionKpiPct($count, $total) {
    if ($total <= 0) return '0%';
    return number_format($count * 100.0 / $total, 1, ',', '.') . '%';
}

function e3dcRenderStorageDecisionDailyKpis() {
    $gridImport = 0; $gridImportCharge = 0; $houseTransient = 0; $freilaufSettling = 0; $stale = 0; $aboveSoft = 0; $aboveUnbounded = 0; $aboveHold = 0; $belowDischarge = 0; $gapAbsSum = 0.0; $gapCount = 0;
    $total = e3dcWalkLatestDecisionHistoryRows('storage_decision_history_', function($row) use (&$gridImport, &$gridImportCharge, &$houseTransient, &$freilaufSettling, &$stale, &$aboveSoft, &$aboveUnbounded, &$aboveHold, &$belowDischarge, &$gapAbsSum, &$gapCount) {
        $r5 = is_array($row['r5'] ?? null) ? $row['r5'] : [];
        $inputs = is_array($row['inputs'] ?? null) ? $row['inputs'] : [];
        $curve = is_array($row['curve'] ?? null) ? $row['curve'] : [];
        $grid = (float)($inputs['grid_w'] ?? 0);
        $bat = (float)($inputs['bat_w'] ?? 0);
        $wallboxW = (float)($inputs['wallbox_w'] ?? 0);
        $decision = is_array($row['decision'] ?? null) ? $row['decision'] : [];
        $state = (string)($decision['state'] ?? '');
        $owner = (string)($r5['control_owner'] ?? '');
        $rawGridCharge = ($grid > 500 && $bat > 100);
        $looksLikeHouseTransient = (
            $rawGridCharge
            && $owner === 'e3dc_auto'
            && abs($wallboxW) <= 100.0
            && in_array($state, ['parallel_auto', 'parallel_grid_relief_auto', 'parallel_evening_release'], true)
        );
        $gap = isset($curve['gap_pct']) && is_numeric($curve['gap_pct']) ? (float)$curve['gap_pct'] : null;
        $limit = array_key_exists('active_charge_limit_w', $r5) && is_numeric($r5['active_charge_limit_w']) ? (float)$r5['active_charge_limit_w'] : null;
        $limitSlack = $limit !== null ? max(300.0, $limit * 0.25) : 0.0;
        if (!empty($r5['grid_import_gt500']) || $grid > 500) $gridImport++;
        if (!empty($r5['house_load_transient']) || $looksLikeHouseTransient) $houseTransient++;
        if (!empty($r5['freilauf_settling_active'])) $freilaufSettling++;
        if (!empty($r5['grid_import_with_battery_charge']) || ($rawGridCharge && !$looksLikeHouseTransient)) $gridImportCharge++;
        if (!empty($r5['live_stale']) || !empty($inputs['live_stale'])) $stale++;
        if ($gap !== null) {
            $gapAbsSum += abs($gap); $gapCount++;
            if (!empty($r5['above_curve_soft_charge']) || ($gap > 1.0 && $bat > 100 && $limit !== null && $bat <= $limit + $limitSlack)) $aboveSoft++;
            if (!empty($r5['above_curve_unbounded_charge']) || ($gap > 1.0 && $bat > 100 && ($limit === null || $bat > $limit + $limitSlack))) $aboveUnbounded++;
            if (!empty($r5['above_curve_hold']) || ($gap > 1.0 && $bat <= 50)) $aboveHold++;
            if (!empty($r5['below_curve_discharge']) || ($gap < -2.0 && $bat < -100)) $belowDischarge++;
        }
    });
    if ($total <= 0) return '';
    $avgGap = $gapCount > 0 ? number_format($gapAbsSum / $gapCount, 2, ',', '.') . '%' : '--';
    return "R5 Tages-KPIs: Ø |Kurvenabweichung| " . $avgGap
        . " | Netzbezug >500W " . $gridImport . " (" . e3dcDecisionKpiPct($gridImport, $total) . ")"
        . " | Netzbezug+Akku laden " . $gridImportCharge
        . " | Hauslast-Transient " . $houseTransient
        . " | Freilauf-Settling " . $freilaufSettling
        . " | stale " . $stale
        . " | oberhalb weich geführt " . $aboveSoft
        . " | oberhalb auffällig " . $aboveUnbounded
        . " | oberhalb gehalten " . $aboveHold
        . " | unterhalb entladen " . $belowDischarge . "\n";
}

function e3dcRenderEnergyDecisionDailyKpis() {
    $offered = 0; $accepted = 0; $configured = 0; $connected = 0;
    $total = e3dcWalkLatestDecisionHistoryRows('energy_decision_history_', function($row) use (&$offered, &$accepted, &$configured, &$connected) {
        $decision = is_array($row['decision'] ?? null) ? $row['decision'] : [];
        $heatpump = is_array($row['heatpump'] ?? null) ? $row['heatpump'] : [];
        $state = (string)($decision['state'] ?? '');
        if (!empty($heatpump['configured'])) $configured++;
        if (!empty($heatpump['connected'])) $connected++;
        if (!empty($heatpump['budget_offered']) || strpos($state, '_boost') !== false || strpos($state, '_budget_frei') !== false || strpos($state, '_aktiv') !== false) $offered++;
        if (!empty($heatpump['accepting_power']) || (float)($heatpump['wp_power_w'] ?? 0) > 100) $accepted++;
    });
    if ($total <= 0) return '';
    return "R5 Tages-KPIs: WP konfiguriert " . e3dcDecisionKpiPct($configured, $total)
        . " | verbunden " . e3dcDecisionKpiPct($connected, $total)
        . " | Budget angeboten " . $offered
        . " | Leistung angenommen " . $accepted . "\n";
}

function e3dcRenderWallboxDecisionDailyKpis() {
    $stale = 0; $timeout = 0; $changes = 0; $setAmp = 0;
    $total = e3dcWalkLatestDecisionHistoryRows('wallbox_decision_history_', function($row) use (&$stale, &$timeout, &$changes, &$setAmp) {
        $decision = is_array($row['decision'] ?? null) ? $row['decision'] : [];
        $inputs = is_array($row['inputs'] ?? null) ? $row['inputs'] : [];
        if (!empty($inputs['budget_stale'])) $stale++;
        if (!empty($inputs['budget_timeout'])) $timeout++;
        if (!empty($decision['made_changes'])) $changes++;
        if ((float)($inputs['set_amp'] ?? 0) > 0) $setAmp++;
    });
    if ($total <= 0) return '';
    return "R5 Tages-KPIs: Budget stale " . $stale
        . " | Timeout " . $timeout
        . " | Schreibaktionen " . $changes
        . " | Sollstrom >0 " . $setAmp . "\n";
}

function e3dcReadStorageDecisionHistoryEvents($limit = 20) {
    return e3dcReadDecisionHistoryEventsByPrefix(
        'storage_decision_history_',
        '/var/www/html/ramdisk/storage_decision_latest.json',
        $limit
    );
}

function e3dcStorageDecisionModeText($decision) {
    if (!is_array($decision)) return '--';
    $mode = trim((string)($decision['mode_name'] ?? ''));
    $val = (int)($decision['val_w'] ?? 0);
    return ($mode !== '' ? $mode : '--') . ' ' . $val . 'W';
}

function e3dcFormatStorageDecisionLine($event) {
    if (!is_array($event)) return '';
    $ts = isset($event['ts']) ? date('d.m. H:i:s', (int)$event['ts']) : '--';
    $decision = is_array($event['decision'] ?? null) ? $event['decision'] : [];
    $inputs = is_array($event['inputs'] ?? null) ? $event['inputs'] : [];
    $curve = is_array($event['curve'] ?? null) ? $event['curve'] : [];
    $wallbox = is_array($event['wallbox'] ?? null) ? $event['wallbox'] : [];
    $r5 = is_array($event['r5'] ?? null) ? $event['r5'] : [];
    $state = trim((string)($decision['label'] ?? $decision['state'] ?? '--'));
    $reason = substr(trim((string)($decision['reason'] ?? '')), 0, 180);
    $soc = isset($inputs['soc']) ? number_format((float)$inputs['soc'], 1, ',', '.') . '%' : '--';
    $curveSoc = isset($curve['soc_now']) && $curve['soc_now'] !== null ? number_format((float)$curve['soc_now'], 1, ',', '.') . '%' : '--';
    $grid = (int)($inputs['grid_w'] ?? 0);
    $pv = (int)($inputs['pv_w'] ?? 0);
    $wbBudget = (int)($wallbox['budget_w'] ?? 0);
    $owner = trim((string)($r5['control_owner'] ?? ''));
    $contract = trim((string)($r5['contract_owner'] ?? ''));
    $execution = trim((string)($r5['execution_class'] ?? ''));
    return $ts . ' | ' . $state . ' | ' . e3dcStorageDecisionModeText($decision)
        . ' | SoC ' . $soc . ' / Kurve ' . $curveSoc
        . ($owner !== '' ? ' | Besitzer ' . $owner : '')
        . ($contract !== '' ? ' | Vertrag ' . $contract : '')
        . ($execution !== '' ? ' | Ausführung ' . $execution : '')
        . ' | Grid ' . $grid . 'W PV ' . $pv . 'W WB frei ' . $wbBudget . 'W'
        . ' | ' . $reason;
}

function e3dcRenderStorageDecisionHistoryText($limit = 30) {
    $events = e3dcReadStorageDecisionHistoryEvents($limit);
    if (empty($events)) {
        return "Noch keine Storage-Entscheidungshistorie vorhanden.\n\nDer neue Recorder schreibt nach dem nächsten Storage-Manager-Zyklus eine JSONL-Zeile pro Entscheidung.";
    }
    $last = $events[0];
    $decision = is_array($last['decision'] ?? null) ? $last['decision'] : [];
    $inputs = is_array($last['inputs'] ?? null) ? $last['inputs'] : [];
    $curve = is_array($last['curve'] ?? null) ? $last['curve'] : [];
    $wallbox = is_array($last['wallbox'] ?? null) ? $last['wallbox'] : [];
    $storageBudget = is_array($last['storage_budget'] ?? null) ? $last['storage_budget'] : [];
    $r5 = is_array($last['r5'] ?? null) ? $last['r5'] : [];
    $trace = is_array($last['trace'] ?? null) ? $last['trace'] : [];
    $out = "--- Speicher-Entscheidungsrecorder ---\n\n";
    $dailyKpis = e3dcRenderStorageDecisionDailyKpis();
    if ($dailyKpis !== '') $out .= $dailyKpis . "\n";
    $out .= "Letzte Entscheidung: " . ($decision['label'] ?? $decision['state'] ?? '--') . "\n";
    $out .= "RSCP: " . e3dcStorageDecisionModeText($decision) . "\n";
    $out .= "Priorität: " . ($decision['priority'] ?? '--') . " | Protected: " . (!empty($decision['protected']) ? 'ja' : 'nein') . "\n";
    $out .= "Regelbesitzer: " . ($r5['control_owner'] ?? '--');
    if (!empty($r5['contract_owner'])) {
        $out .= " | Vertrag: " . $r5['contract_owner'];
    }
    if (!empty($r5['execution_class'])) {
        $out .= " | Ausführung: " . $r5['execution_class'];
    }
    if (array_key_exists('active_charge_limit_w', $r5) && $r5['active_charge_limit_w'] !== null) {
        $out .= " | aktive Ladegrenze: " . (int)$r5['active_charge_limit_w'] . " W";
    }
    $out .= "\n";
    if (!empty($r5['above_curve_soft_charge']) || !empty($r5['above_curve_unbounded_charge']) || !empty($r5['grid_import_with_battery_charge']) || !empty($r5['house_load_transient']) || !empty($r5['freilauf_settling_active'])) {
        $r5Flags = [];
        if (!empty($r5['above_curve_soft_charge'])) $r5Flags[] = 'oberhalb weich geführt';
        if (!empty($r5['above_curve_unbounded_charge'])) $r5Flags[] = 'oberhalb auffällig';
        if (!empty($r5['grid_import_with_battery_charge'])) $r5Flags[] = 'Netzbezug mit Batterieladung';
        if (!empty($r5['house_load_transient'])) $r5Flags[] = 'Hauslast-Transient im E3DC-AUTO';
        if (!empty($r5['freilauf_settling_active'])) $r5Flags[] = 'Freilauf-Settling';
        $out .= "R5-Einstufung: " . implode(', ', $r5Flags) . "\n";
    }
    $out .= "SoC/Kurve: " . ($inputs['soc'] ?? '--') . "% / " . ($curve['soc_now'] ?? '--') . "% | Ziel: " . ($curve['target_soc'] ?? '--') . "%\n";
    $out .= "PV/Grid/Akku/Haus/WP/WB: "
        . (int)($inputs['pv_w'] ?? 0) . "W / "
        . (int)($inputs['grid_w'] ?? 0) . "W / "
        . (int)($inputs['bat_w'] ?? 0) . "W / "
        . (int)($inputs['home_w'] ?? 0) . "W / "
        . (int)($inputs['wp_w'] ?? 0) . "W / "
        . (int)($inputs['wallbox_w'] ?? 0) . "W\n";
    $out .= "Speicherbudget: Rahmen=" . (int)($storageBudget['storage_charge_request_w'] ?? $storageBudget['storage_req_w'] ?? 0)
        . "W, iFc=" . (int)($storageBudget['iFc_w'] ?? 0)
        . "W, iMin=" . (int)($storageBudget['iMinLade_w'] ?? 0)
        . "W, Verbraucher frei=" . (int)($storageBudget['free_for_consumers_w'] ?? 0) . "W\n";
    $out .= "Wallbox: Budget=" . (int)($wallbox['budget_w'] ?? 0)
        . "W, möglich=" . (int)($wallbox['possible_power_w'] ?? 0)
        . "W, ladbar=" . (
            array_key_exists('physical_chargeable', $wallbox) && $wallbox['physical_chargeable'] !== null
                ? ($wallbox['physical_chargeable'] ? 'ja' : 'nein')
                : '--'
        ) . "\n";
    $out .= "Grund: " . ($decision['reason'] ?? '--') . "\n\n";
    if (!empty($trace)) {
        $out .= "--- Letzte interne Trace-Schritte ---\n";
        foreach ($trace as $entry) {
            if (!is_array($entry)) continue;
            $out .= '- ' . ($entry['step'] ?? '--');
            if (isset($entry['action'])) $out .= ' / ' . $entry['action'];
            if (isset($entry['mode'])) $out .= ' / ' . $entry['mode'];
            if (isset($entry['val'])) $out .= ' ' . $entry['val'] . 'W';
            if (isset($entry['reason'])) $out .= ' | ' . $entry['reason'];
            $out .= "\n";
        }
        $out .= "\n";
    }
    $out .= "--- Letzte Entscheidungen ---\n";
    foreach ($events as $event) {
        $out .= e3dcFormatStorageDecisionLine($event) . "\n";
    }
    return $out;
}

function e3dcReadEmsReactionHistoryEvents($limit = 20) {
    return e3dcReadDecisionHistoryEventsByPrefix(
        'ems_reaction_history_',
        '/var/www/html/ramdisk/ems_reaction_latest.json',
        $limit
    );
}

function e3dcEmsReactionStatusLabel($status) {
    $status = (string)$status;
    $map = [
        'keine_ems_vorgabe' => 'keine EMS-Vorgabe',
        'ziel_eingehalten' => 'Ziel bereits eingehalten',
        'reagiert' => 'reagiert',
        'wartet' => 'wartet auf Akkuantwort',
        'keine_sichtbare_reaktion' => 'keine sichtbare Reaktion',
        'e3dc_autonom_lädt' => 'E3DC lädt autonom',
        'e3dc_lädt_trotz_limit' => 'E3DC lädt trotz Limit',
        'freigegeben' => 'EMS freigegeben',
        'wartet_freigabe' => 'wartet auf Freigabe',
        'grenze_überschritten' => 'Grenze überschritten',
    ];
    return $map[$status] ?? ($status !== '' ? $status : '--');
}

function e3dcFormatEmsReactionLine($event) {
    if (!is_array($event)) return '';
    $ts = isset($event['ts']) ? date('d.m. H:i:s', (int)$event['ts']) : '--';
    $command = is_array($event['command'] ?? null) ? $event['command'] : [];
    $current = is_array($event['current'] ?? null) ? $event['current'] : [];
    $reaction = is_array($event['reaction'] ?? null) ? $event['reaction'] : [];
    $context = is_array($event['context'] ?? null) ? $event['context'] : [];
    $kind = (string)($command['kind'] ?? '--');
    $maxCharge = (int)($command['max_charge_w'] ?? 0);
    $maxDischarge = (int)($command['max_discharge_w'] ?? 0);
    $status = e3dcEmsReactionStatusLabel($reaction['status'] ?? '');
    $reactionS = $reaction['reaction_s'] ?? null;
    $reactionText = is_numeric($reactionS) ? number_format((float)$reactionS, 1, ',', '.') . 's' : '--';
    return $ts . ' | ' . $status
        . ' | ' . $kind . ' Ladegrenze=' . $maxCharge . 'W Entladegrenze=' . $maxDischarge . 'W'
        . ' | Akku=' . (int)($current['bat_w'] ?? 0) . 'W Netz=' . (int)($current['grid_w'] ?? 0) . 'W PV=' . (int)($current['pv_w'] ?? 0) . 'W'
        . ' | Reaktion=' . $reactionText
        . ' | ' . ($context['label'] ?? $context['state'] ?? '--');
}

function e3dcRenderEmsReactionHistoryText($limit = 30) {
    $events = e3dcReadEmsReactionHistoryEvents($limit);
    if (empty($events)) {
        return "Noch keine EMS-Reaktionszeitdaten vorhanden.\n\nDer Storage Manager schreibt nach dem nächsten Zyklus die letzte EMS-Vorgabe und die beobachtete Akkuantwort.";
    }
    $last = $events[0];
    $command = is_array($last['command'] ?? null) ? $last['command'] : [];
    $before = is_array($last['before'] ?? null) ? $last['before'] : [];
    $current = is_array($last['current'] ?? null) ? $last['current'] : [];
    $live = is_array($last['live_ems'] ?? null) ? $last['live_ems'] : [];
    $reaction = is_array($last['reaction'] ?? null) ? $last['reaction'] : [];
    $context = is_array($last['context'] ?? null) ? $last['context'] : [];
    $reactionS = $reaction['reaction_s'] ?? null;
    $out = "--- EMS-Reaktionszeit ---\n\n";
    $out .= "Letzte Vorgabe: " . ($command['kind'] ?? '--')
        . " | LimitsUsed=" . (!empty($command['limits_used']) ? 'ja' : 'nein')
        . " | Laden max. " . (int)($command['max_charge_w'] ?? 0) . " W"
        . " | Entladen max. " . (int)($command['max_discharge_w'] ?? 0) . " W\n";
    $out .= "Status: " . e3dcEmsReactionStatusLabel($reaction['status'] ?? '')
        . " | Ladepfad: " . e3dcEmsReactionStatusLabel($reaction['charge_status'] ?? '')
        . " | Entladepfad: " . e3dcEmsReactionStatusLabel($reaction['discharge_status'] ?? '') . "\n";
    $out .= "Reaktionszeit: " . (is_numeric($reactionS) ? number_format((float)$reactionS, 1, ',', '.') . " s" : "--")
        . " | Alter der Vorgabe: " . number_format((float)($command['age_s'] ?? 0), 1, ',', '.') . " s\n";
    $out .= "Vorher: Akku " . (int)($before['bat_w'] ?? 0) . " W | Netz " . (int)($before['grid_w'] ?? 0) . " W | PV " . (int)($before['pv_w'] ?? 0) . " W\n";
    $out .= "Aktuell: Akku " . (int)($current['bat_w'] ?? 0) . " W | Netz " . (int)($current['grid_w'] ?? 0) . " W | PV " . (int)($current['pv_w'] ?? 0) . " W | SoC " . ($current['soc'] ?? '--') . "%\n";
    $out .= "RSCP-Rückmeldung: Limits aktiv=" . (
        array_key_exists('power_limits_active', $live)
            ? (!empty($live['power_limits_active']) ? 'ja' : 'nein')
            : '--'
        )
        . " | EMS max. Laden=" . (isset($live['ems_max_charge_power_w']) ? (int)$live['ems_max_charge_power_w'] . " W" : "--")
        . " | genutzt=" . (isset($live['used_charge_limit_w']) ? (int)$live['used_charge_limit_w'] . " W" : "--") . "\n";
    $out .= "Kontext: " . ($context['label'] ?? $context['state'] ?? '--')
        . " | Abregel aktiv=" . (!empty($context['abregel_active']) ? 'ja' : 'nein')
        . " | Druck=" . (int)($context['abregel_pressure_w'] ?? 0) . " W\n";
    $out .= "Hinweis: " . ($reaction['note'] ?? '--') . "\n";
    $out .= "Grund: " . ($command['reason'] ?? '--') . "\n\n";
    $out .= "--- Letzte Messpunkte ---\n";
    foreach ($events as $event) {
        $out .= e3dcFormatEmsReactionLine($event) . "\n";
    }
    return $out;
}

function e3dcReadEnergyDecisionHistoryEvents($limit = 20) {
    return e3dcReadDecisionHistoryEventsByPrefix(
        'energy_decision_history_',
        '/var/www/html/ramdisk/energy_decision_latest.json',
        $limit
    );
}

function e3dcHeatDecisionOwnerLabel($owner) {
    $owner = strtolower(trim((string)$owner));
    switch ($owner) {
        case 'predump_heatpump': return 'Pre-Dump';
        case 'market_plan_heatpump': return 'Marktfenster';
        case 'price_plan_heatpump': return 'Preisfenster';
        case 'legacy_price_heatpump': return 'Preis-Boost Legacy';
        case 'legacy_pv_pause':
        case 'source_recovery_heatpump':
        case 'quell_erholung':
            return 'Quell-Erholung';
        case 'manual_heatpump':
        case 'manual_ww_heatpump':
            return 'Manuell';
        case 'storage_budget_heatpump': return 'Wärmebudget';
        case 'none':
        case '':
            return 'Beobachtet';
        default: return $owner;
    }
}

function e3dcFormatEnergyDecisionLine($event) {
    if (!is_array($event)) return '';
    $ts = isset($event['ts']) ? date('d.m. H:i:s', (int)$event['ts']) : '--';
    $decision = is_array($event['decision'] ?? null) ? $event['decision'] : [];
    $inputs = is_array($event['inputs'] ?? null) ? $event['inputs'] : [];
    $heatpump = is_array($event['heatpump'] ?? null) ? $event['heatpump'] : [];
    $state = trim((string)($decision['state'] ?? '--'));
    $ownerLabel = e3dcHeatDecisionOwnerLabel($decision['heatpump_boost_owner'] ?? ($heatpump['owner'] ?? 'none'));
    $free = (int)($inputs['free_for_limbs_w'] ?? 0);
    $hpBudget = $inputs['heatpump_budget_w'] ?? null;
    $hold = (int)($heatpump['predump_hold_remaining_s'] ?? 0);
    $grid = (int)($inputs['grid_w'] ?? 0);
    $wp = (int)($heatpump['wp_power_w'] ?? 0);
    $suffix = '';
    if ($hold > 0) $suffix .= ' | Pre-Dump-Haltezeit ' . ceil($hold / 60) . ' min';
    if (!empty($heatpump['targets_reached'])) $suffix .= ' | Zieltemperatur erreicht';
    if (!empty($heatpump['protect_block'])) $suffix .= ' | WQ-Schutz';
    return $ts . ' | ' . $state . ' | Besitzer=' . $ownerLabel . ' | Budget=' . $free . 'W'
        . ($hpBudget !== null ? ' (WP ' . (int)$hpBudget . 'W)' : '')
        . ' | Netz=' . $grid . 'W | WP=' . $wp . 'W'
        . ' | ' . ($decision['reason'] ?? '--') . $suffix;
}

function e3dcRenderEnergyDecisionHistoryText($limit = 20) {
    $events = e3dcReadEnergyDecisionHistoryEvents($limit);
    if (empty($events)) {
        return "Noch keine Wärme-Entscheidungen vorhanden.\n\nSobald der Energy Manager läuft, erscheinen hier Budget, Wärme-Besitzer und Schutzgründe.";
    }
    $last = $events[0];
    $decision = is_array($last['decision'] ?? null) ? $last['decision'] : [];
    $inputs = is_array($last['inputs'] ?? null) ? $last['inputs'] : [];
    $heatpump = is_array($last['heatpump'] ?? null) ? $last['heatpump'] : [];
    $ownerLabel = e3dcHeatDecisionOwnerLabel($decision['heatpump_boost_owner'] ?? ($heatpump['owner'] ?? 'none'));
    $out = "--- Wärme-Entscheidungen ---\n\n";
    $dailyKpis = e3dcRenderEnergyDecisionDailyKpis();
    if ($dailyKpis !== '') $out .= $dailyKpis . "\n";
    $out .= "Letzte Entscheidung: " . ($decision['state'] ?? '--') . "\n";
    $out .= "Wärme-Besitzer: " . $ownerLabel . "\n";
    $out .= "Grund: " . ($decision['reason'] ?? '--') . "\n";
    $out .= "Speicher-Budget: " . (int)($inputs['free_for_limbs_w'] ?? 0) . " W\n";
    $out .= "WP-Budget: " . (isset($inputs['heatpump_budget_w']) ? (int)$inputs['heatpump_budget_w'] . " W" : "--") . "\n";
    $out .= "Pre-Dump aktiv: " . (!empty($heatpump['predump_active']) ? "ja" : "nein") . "\n";
    $out .= "Haltezeit Rest: " . (int)($heatpump['predump_hold_remaining_s'] ?? 0) . " s\n";
    $out .= "Schutz: " . (!empty($heatpump['protect_block']) ? "WQ-Schutz" : "OK") . "\n\n";
    $actions = is_array($decision['actions'] ?? null) ? $decision['actions'] : [];
    if (!empty($actions)) {
        $out .= "--- Aktionen im letzten Zyklus ---\n";
        foreach ($actions as $action) {
            if (!is_array($action)) continue;
            $out .= '- ' . ($action['action'] ?? '--') . ' / ' . ($action['owner'] ?? '--');
            if (isset($action['min_runtime_s'])) $out .= ' / Laufzeit ' . (int)$action['min_runtime_s'] . 's';
            if (isset($action['reason'])) $out .= ' / ' . $action['reason'];
            $out .= "\n";
        }
        $out .= "\n";
    }
    $out .= "--- Letzte Entscheidungen ---\n";
    foreach ($events as $event) {
        $out .= e3dcFormatEnergyDecisionLine($event) . "\n";
    }
    return $out;
}

function e3dcReadWallboxDecisionHistoryEvents($limit = 20) {
    return e3dcReadDecisionHistoryEventsByPrefix(
        'wallbox_decision_history_',
        '/var/www/html/ramdisk/wallbox_decision_latest.json',
        $limit
    );
}

function e3dcFormatWallboxDecisionLine($event) {
    if (!is_array($event)) return '';
    $ts = isset($event['ts']) ? date('d.m. H:i:s', (int)$event['ts']) : '--';
    $decision = is_array($event['decision'] ?? null) ? $event['decision'] : [];
    $inputs = is_array($event['inputs'] ?? null) ? $event['inputs'] : [];
    $state = trim((string)($decision['state'] ?? '--'));
    $budget = (int)($inputs['effective_budget_w'] ?? 0);
    $cap = (int)($inputs['cap_amp'] ?? 0);
    $set = (int)($inputs['set_amp'] ?? 0);
    $battery = trim((string)($decision['battery_request'] ?? '--'));
    $reason = substr(trim((string)($decision['reason'] ?? '')), 0, 160);
    return $ts . ' | ' . $state . ' | Budget=' . $budget . 'W | Cap=' . $cap . 'A | Soll=' . $set . 'A | Akku=' . $battery . ' | ' . $reason;
}

function e3dcRenderWallboxDecisionHistoryText($limit = 20) {
    $events = e3dcReadWallboxDecisionHistoryEvents($limit);
    if (empty($events)) {
        return "Noch keine Wallbox-Entscheidungen vorhanden.\n\nSobald der Wallbox Manager läuft, erscheinen hier Budget, Start-/Stopgrund und Zustand je Wallbox.";
    }
    $last = $events[0];
    $decision = is_array($last['decision'] ?? null) ? $last['decision'] : [];
    $inputs = is_array($last['inputs'] ?? null) ? $last['inputs'] : [];
    $wallboxes = is_array($last['wallboxes'] ?? null) ? $last['wallboxes'] : [];
    $out = "--- Wallbox-Entscheidungen ---\n\n";
    $dailyKpis = e3dcRenderWallboxDecisionDailyKpis();
    if ($dailyKpis !== '') $out .= $dailyKpis . "\n";
    $out .= "Letzte Entscheidung: " . ($decision['state'] ?? '--') . "\n";
    $out .= "Grund: " . ($decision['reason'] ?? '--') . "\n";
    $out .= "Budget: " . (int)($inputs['effective_budget_w'] ?? 0) . " W\n";
    $out .= "Sollstrom: " . (int)($inputs['set_amp'] ?? 0) . " A\n";
    $out .= "Akku-Anforderung: " . ($decision['battery_request'] ?? '--') . "\n\n";
    if (!empty($wallboxes)) {
        $out .= "--- Wallboxen ---\n";
        foreach ($wallboxes as $wb) {
            if (!is_array($wb)) continue;
            $out .= 'WB' . (int)($wb['id'] ?? 0) . ': ' . ($wb['state'] ?? '--')
                . ' | ' . (int)($wb['amp'] ?? 0) . 'A'
                . ' | ' . (int)($wb['power_w'] ?? 0) . "W";
            if (!empty($wb['state_reason'])) $out .= ' | ' . $wb['state_reason'];
            if (!empty($wb['rscp_error_active']) && !empty($wb['rscp_last_error'])) {
                $out .= ' | RSCP-Fehler: ' . $wb['rscp_last_error'];
            }
            $out .= "\n";
        }
        $out .= "\n";
    }
    $out .= "--- Letzte Entscheidungen ---\n";
    foreach ($events as $event) {
        $out .= e3dcFormatWallboxDecisionLine($event) . "\n";
    }
    return $out;
}

function e3dcEnsureUtf8Text($text) {
    $text = (string)$text;
    if ($text === '' || preg_match('//u', $text)) {
        return $text;
    }
    if (function_exists('mb_convert_encoding')) {
        $converted = @mb_convert_encoding($text, 'UTF-8', 'Windows-1252');
        if (is_string($converted) && preg_match('//u', $converted)) {
            return $converted;
        }
    }
    if (function_exists('iconv')) {
        $converted = @iconv('Windows-1252', 'UTF-8//TRANSLIT', $text);
        if (is_string($converted) && preg_match('//u', $converted)) {
            return $converted;
        }
    }
    $sanitized = preg_replace('/[^\x09\x0A\x0D\x20-\x7E]/', '?', $text);
    return is_string($sanitized) ? $sanitized : $text;
}

function handleSystemLog() {
    if (isset($_GET['action']) && $_GET['action'] === 'get_system_log') {
        requireWebAuth(true);
        header('Content-Type: text/plain; charset=utf-8');
        $logType = $_GET['log'] ?? '';

        $logFiles = [
            'storage_manager'=> '/var/www/html/logs/storage_manager.log',
            'storage_simulator'=> '/var/www/html/logs/storage_simulator.log',
            'energy_manager' => '/var/www/html/logs/energy_manager.log',
            'wallbox_manager'=> '/var/www/html/logs/wallbox_manager.log',
            'ha_manager'     => '/var/www/html/logs/ha_manager.log',
            'watchdog'       => '/var/www/html/logs/piguard.log',
            'notifier'       => '/var/www/html/logs/notification_manager.log',
            'bluelink'       => '/var/www/html/logs/bluelink_client.log',
            'mqtt_hub'       => '/var/www/html/logs/e3dc_mqtt_hub.log',
            'websocket'      => '/var/www/html/logs/e3dc_websocket.log',
            'update'         => '/var/www/html/logs/update.log',
            'self_update'    => '/var/log/e3dc-control/web-update.log'
        ];

        if ($logType === 'docker') {
            if (file_exists('/.dockerenv')) {
                echo "Das System läuft in Docker.\n\nDas C++ Kern-Log (und andere Container-Ausgaben) können über die Konsole des Hosts abgerufen werden:\n";
                echo "=> sudo docker logs --tail 100 e3dc-control\n";
            } else {
                echo "Das System läuft nativ (Bare-Metal), nicht in Docker.";
            }
            exit;
        }

        if ($logType === 'watchtower') {
            if (file_exists('/.dockerenv')) {
                echo "Das System läuft in Docker.\n\nDas Watchtower-Log (Auto-Updater) kann über die Konsole des Hosts abgerufen werden:\n";
                echo "=> sudo docker logs --tail 100 watchtower\n";
            } else {
                echo "Watchtower ist nur bei Docker-Installationen verfügbar.";
            }
            exit;
        }

        if ($logType === 'wallbox_command_gate') {
            echo e3dcRenderWallboxCommandGateText(30);
            exit;
        }

        if ($logType === 'storage_decision_history') {
            echo e3dcRenderStorageDecisionHistoryText(40);
            exit;
        }

        if ($logType === 'ems_reaction_history') {
            echo e3dcRenderEmsReactionHistoryText(40);
            exit;
        }

        if ($logType === 'energy_decision_history') {
            echo e3dcRenderEnergyDecisionHistoryText(40);
            exit;
        }

        if ($logType === 'wallbox_decision_history') {
            echo e3dcRenderWallboxDecisionHistoryText(40);
            exit;
        }

        if ($logType === 'config_validation') {
            $file = '/var/www/html/ramdisk/config_validation.json';
            if (!file_exists($file)) {
                echo "Noch keine Config-Validator-Daten vorhanden.\n\nDer Storage Manager schreibt diese Datei im laufenden Betrieb in die Ramdisk.";
                exit;
            }
            $data = @json_decode(file_get_contents($file), true);
            if (!is_array($data)) {
                echo "Config-Validator-Daten konnten nicht gelesen werden.";
                exit;
            }

            $summary = $data['summary'] ?? [];
            $warnings = (int)($summary['warnings'] ?? 0);
            echo "--- Config-Validator Zusammenfassung ---\n\n";
            echo "Warnungen: " . $warnings . "\n";
            echo "Quelle: " . ($summary['source_order'] ?? 'unbekannt') . "\n";
            echo "Live-Daten: " . (!empty($summary['live_available']) ? "vorhanden" : "nicht vorhanden") . "\n";
            if (!empty($data['ts'])) {
                echo "Stand: " . date('d.m.Y H:i:s', (int)$data['ts']) . "\n";
            }

            $groupLabels = [
                'storage' => 'Speicher',
                'wallbox' => 'Wallbox',
                'consumer' => 'Verbraucher',
                'price' => 'Preise'
            ];
            foreach ($groupLabels as $group => $label) {
                $items = $data[$group] ?? [];
                if (!is_array($items)) continue;
                $groupWarnings = [];
                foreach ($items as $key => $item) {
                    if (!is_array($item) || ($item['severity'] ?? 'ok') !== 'warning') continue;
                    $groupWarnings[$key] = $item;
                }
                echo "\n--- " . $label . " ---\n";
                if (empty($groupWarnings)) {
                    echo "OK\n";
                    continue;
                }
                foreach (array_slice($groupWarnings, 0, 12, true) as $key => $item) {
                    $name = $item['label'] ?? $key;
                    $message = $item['message'] ?? 'Bitte Eingabe prüfen.';
                    $effective = array_key_exists('effective', $item) ? $item['effective'] : null;
                    $unit = $item['unit'] ?? '';
                    echo "- " . $name . " (" . $key . "): " . $message;
                    if ($effective !== null && $effective !== '') {
                        echo " [wirksam: " . $effective . ($unit ? " " . $unit : "") . "]";
                    }
                    echo "\n";
                }
                if (count($groupWarnings) > 12) {
                    echo "... weitere Warnungen in der Konfiguration sichtbar.\n";
                }
            }
            exit;
        }

        if ($logType === 'wp_status') {
            $luxFile = '/var/www/html/ramdisk/luxtronik.json';
            $idmFile = '/var/www/html/ramdisk/waermepumpe.json';
            $file = file_exists($idmFile) ? $idmFile : $luxFile;

            if (file_exists($file)) {
                $data = @json_decode(file_get_contents($file), true);
                if ($data) {
                    echo "--- Wärmepumpe Status & Diagnose ---\n\n";
                    echo "Quelle: " . basename($file) . "\n";
                    $wpTimestamp = null;
                    $wpTimestampRaw = $data['ts'] ?? null;
                    if (is_numeric($wpTimestampRaw)) {
                        $wpTimestamp = (float)$wpTimestampRaw;
                        if ($wpTimestamp > 100000000000.0) {
                            $wpTimestamp /= 1000.0;
                        }
                        $wpTimestamp = $wpTimestamp > 0 ? (int)floor($wpTimestamp) : null;
                    } elseif (is_string($wpTimestampRaw) && trim($wpTimestampRaw) !== '') {
                        $parsedWpTimestamp = strtotime($wpTimestampRaw);
                        $wpTimestamp = $parsedWpTimestamp !== false ? (int)$parsedWpTimestamp : null;
                    }
                    echo "Letztes Update: " . ($wpTimestamp !== null ? date('d.m.Y H:i:s', $wpTimestamp) : 'Unbekannt') . "\n";
                    if (array_key_exists('success', $data)) {
                        echo "Verbindung: " . (!empty($data['success']) ? "Erfolgreich" : "FEHLGESCHLAGEN") . "\n";
                    } elseif (!empty($data['error'])) {
                        echo "Verbindung: FEHLER GEMELDET\n";
                    } else {
                        echo "Verbindung: Statusdaten vorhanden; kein separates Erfolgsfeld gemeldet\n";
                    }
                    if (!empty($data['error'])) echo "System-Meldung: " . $data['error'] . "\n";

                    // Fehlerhistorie aus RAM-Disk (ws_data_error.json)
                    $wsErrFile = '/var/www/html/ramdisk/ws_data_error.json';
                    if (file_exists($wsErrFile)) {
                        $wsData = @json_decode(file_get_contents($wsErrFile), true);
                        if ($wsData) {
                            echo "\n--- Wärmepumpe Fehler-Historie ---\n";
                            krsort($wsData);
                            foreach ($wsData as $log) {
                                echo $log . "\n";
                            }
                        }
                    }
                } else { echo "Fehler beim Lesen der Daten-Datei."; }
            } else { echo "Keine Wärmepumpen-Daten vorhanden (waermepumpe.json/luxtronik.json fehlt)."; }
            exit;
        }

        if ($logType === 'wp_raw') {
            $luxFile = '/var/www/html/ramdisk/luxtronik.json';
            $idmFile = '/var/www/html/ramdisk/waermepumpe.json';
            $file = file_exists($idmFile) ? $idmFile : $luxFile;
            if (file_exists($file)) {
                $data = @json_decode(file_get_contents($file), true);
                if ($data) {
                    echo "--- Rohdaten (" . basename($file) . ") ---\n\n";
                    echo json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE);
                } else { echo "Fehler beim Lesen der JSON-Datei."; }
            } else { echo "Keine Wärmepumpen-Daten vorhanden."; }
            exit;
        }

        if (!array_key_exists($logType, $logFiles)) {
            echo "Ungültiger Log-Typ.";
            exit;
        }

        $isDocker = file_exists('/.dockerenv');
        $journalServices = [
            'storage_manager'   => ['type' => 'unit', 'name' => 'e3dc-storage-manager'],
            'storage_simulator' => ['type' => 'unit', 'name' => 'e3dc-storage-simulator'],
            'watchdog' => ['type' => 'tag', 'name' => 'PIGUARD'],
            'notifier' => ['type' => 'unit', 'name' => 'e3dc-notifier'],
            'bluelink' => ['type' => 'unit', 'name' => 'e3dc-bluelink'],
            'websocket' => ['type' => 'unit', 'name' => 'e3dc-websocket']
        ];

        if (!$isDocker && array_key_exists($logType, $journalServices)) {
            $j = $journalServices[$logType];
            if ($j['type'] === 'tag') {
                passthru("journalctl -t " . escapeshellarg($j['name']) . " -n 150 --no-pager --reverse 2>&1");
            } else {
                passthru("journalctl -u " . escapeshellarg($j['name']) . " -n 150 --no-pager --reverse 2>&1");
            }
            exit;
        }

        $logFile = $logFiles[$logType];
        if (file_exists($logFile) && is_readable($logFile)) {
            $lines = e3dcReadTextTailLines($logFile, 150, 1024 * 1024, true);
            if ($lines) {
                $last_lines = $lines;

                echo "--- Letzte " . count($last_lines) . " Einträge aus " . basename($logFile) . " ---\n\n";
                // Update-Logs sequenziell (richtig herum) anzeigen, Endlos-Logs umkehren (neueste oben)
                if ($logType === 'update' || $logType === 'self_update') {
                    echo implode("\n", $last_lines);
                } else {
                    echo implode("\n", array_reverse($last_lines));
                }
            } else {
                echo "Log-Datei ist leer: " . basename($logFile);
            }
        } else {
            if ($logType === 'self_update') {
                echo "Bisher wurde kein Web-Update durchgeführt (Log existiert noch nicht).";
            } else {
                echo "Log-Datei nicht gefunden oder nicht lesbar: " . basename($logFile);
            }
        }
        exit;
    }
}

/**
 * Prüft den Dateizugriff und zeigt eine benutzerfreundliche Fehlermeldung
 *
 * @param string $path Dateipfad
 * @param string $operation 'read', 'write', or 'exists'
 * @return bool|string true bei Erfolg, oder HTML mit Fehlermeldung
 */
function checkFileAccess($path, $operation = 'read') {
    // Sicherheit: Nur absolute Pfade oder relative im bekannten Verzeichnis
    if (strpos($path, '..') !== false) {
        return errorMessage("Ungültiger Pfad: Pfad-Traversal erkannt.");
    }

    if (!file_exists($path)) {
        return errorMessage(
            "Datei nicht gefunden",
            "Der Zugriff auf <code>" . htmlspecialchars($path) . "</code> ist fehlgeschlagen. " .
            "Möglicherweise existiert die Datei nicht oder der Pfad ist falsch."
        );
    }

    if ($operation === 'read' && !is_readable($path)) {
        return errorMessage(
            "Leseberechtigung fehlt",
            "Die Datei <code>" . htmlspecialchars($path) . "</code> kann nicht gelesen werden. " .
            "Bitte prüfen Sie die Dateiberechtigungen (chmod 755 oder 644)."
        );
    }

    if ($operation === 'write' && !is_writable($path)) {
        $parent = dirname($path);
        $isParentWritable = is_writable($parent) ? "ja" : "nein";

        return errorMessage(
            "Schreibberechtigung fehlt",
            "Die Datei <code>" . htmlspecialchars($path) . "</code> kann nicht geschrieben werden. " .
            "Bitte prüfen Sie die Dateiberechtigungen. Eltern-Verzeichnis schreibbar: " . $isParentWritable
        );
    }

    if ($operation === 'mkdir' && !is_dir($path)) {
        if (!@mkdir($path, 0755, true)) {
            return errorMessage(
                "Verzeichnis konnte nicht erstellt werden",
                "Das Verzeichnis <code>" . htmlspecialchars($path) .
                "</code> existiert nicht und konnte nicht erstellt werden."
            );
        }
    }

    return true;
}

/**
 * Formatiert eine Fehlermeldung als HTML-Box
 */
function errorMessage($title, $details = '') {
    $html = '<div class="error-box" style="background:var(--bs-danger-bg-subtle, #f8d7da); border-left:4px solid var(--bs-danger, #e74c3c); padding:20px; margin:20px 0; border-radius:4px;">';
    $html .= '<h3 style="color:var(--bs-danger-text-emphasis, #842029); margin-top:0;">' . htmlspecialchars($title) . '</h3>';
    if ($details) {
        $html .= '<p style="margin:10px 0 0 0; color:var(--bs-body-color, #334155); line-height:1.6;">' . $details . '</p>';
    }
    $html .= '</div>';
    return $html;
}

// ==================== INSTALL PATH ====================

/** Liest eine kleine reguläre JSON-Metadatendatei; symbolische Links sind keine Autorität. */
function e3dcReadPathMetadata($file) {
    if (!is_string($file) || $file === '' || !is_file($file) || is_link($file) || !is_readable($file)) {
        return [];
    }
    $size = @filesize($file);
    if ($size === false || $size < 2 || $size > 1048576) return [];
    $data = @json_decode((string)@file_get_contents($file), true);
    if (!is_array($data)) return [];
    if (isset($data['config']) && is_array($data['config'])) {
        $nested = $data['config'];
        unset($data['config']);
        $data = array_replace($nested, $data);
    }
    return $data;
}

/** Prüft einen bestehenden Produktstamm anhand expliziter oder persistierter Autorität. */
function e3dcValidatedProductRoot($candidate) {
    $candidate = trim((string)$candidate);
    if ($candidate === '' || !str_starts_with($candidate, '/') || strpos($candidate, "\0") !== false) return null;
    $resolved = @realpath($candidate);
    if ($resolved === false || !is_dir($resolved)) return null;
    foreach (['VERSION', 'installer_main.py', 'Installer/installer_config.py'] as $marker) {
        if (!is_file($resolved . '/' . $marker)) return null;
    }
    return rtrim($resolved, '/');
}

function e3dcInvalidInstallPaths($reason) {
    return [
        'valid' => false,
        'error' => (string)$reason,
        'install_user' => '',
        'install_path' => '',
        'home_dir' => '',
        'venv_path' => '',
    ];
}

/**
 * Löst den exakten Release-Stamm aus expliziter Autorität oder Installermetadaten auf.
 * Benutzerverzeichnisse werden nicht durchsucht; Konto-, Installations- und Venv-Pfade werden nicht geraten.
 */
function getInstallPaths() {
    $v4File = '/var/www/html/data/e3dc_v4.json';
    $legacyFile = '/var/www/html/e3dc_paths.json';
    $v4Data = e3dcReadPathMetadata($v4File);
    $legacyData = e3dcReadPathMetadata($legacyFile);
    $metadata = !empty($v4Data['install_path']) ? $v4Data : $legacyData;

    $explicitRoot = trim((string)(getenv('E3DC_INSTALL_ROOT') ?: ''));
    $configuredRoot = trim((string)($metadata['install_path'] ?? ''));
    $sourceRoot = e3dcValidatedProductRoot(dirname(__DIR__));
    $containerRoot = is_file('/.dockerenv') ? e3dcValidatedProductRoot('/app/pi/Install') : null;
    $authoritativeRaw = $explicitRoot !== '' ? $explicitRoot : $configuredRoot;
    if ($authoritativeRaw !== '') {
        $installRoot = e3dcValidatedProductRoot($authoritativeRaw);
        if ($installRoot === null) return e3dcInvalidInstallPaths('Installationsmetadaten sind ungültig.');
    } else {
        $installRoot = $containerRoot ?: $sourceRoot;
    }
    if ($installRoot === null) return e3dcInvalidInstallPaths('Installationskontext fehlt.');
    if ($sourceRoot !== null && $sourceRoot !== $installRoot) {
        return e3dcInvalidInstallPaths('Installationsmetadaten und ausgefuehrter Release-Baum widersprechen sich.');
    }

    $installUser = trim((string)(getenv('E3DC_INSTALL_USER') ?: ($metadata['install_user'] ?? '')));
    if ($installUser === '' && function_exists('posix_getpwuid')) {
        $owner = @posix_getpwuid((int)@fileowner($installRoot));
        if (is_array($owner)) $installUser = trim((string)($owner['name'] ?? ''));
    }
    if ($installUser === '' || !preg_match('/^[A-Za-z0-9_.-]+$/', $installUser)) {
        return e3dcInvalidInstallPaths('Installationsbenutzer fehlt oder ist ungültig.');
    }

    $homeDir = trim((string)(getenv('E3DC_HOME_DIR') ?: ($metadata['home_dir'] ?? '')));
    if ($homeDir === '' && function_exists('posix_getpwnam')) {
        $account = @posix_getpwnam($installUser);
        if (is_array($account)) $homeDir = trim((string)($account['dir'] ?? ''));
    }
    if ($homeDir === '' || !str_starts_with($homeDir, '/') || !is_dir($homeDir)) {
        return e3dcInvalidInstallPaths('Home-Verzeichnis fehlt oder ist ungültig.');
    }
    $homeDir = (string)@realpath($homeDir);
    if ($homeDir === '') return e3dcInvalidInstallPaths('Home-Verzeichnis ist nicht aufloesbar.');

    $venvPath = trim((string)(getenv('E3DC_VENV_PATH') ?: ($metadata['venv_path'] ?? '')));
    if ($venvPath !== '' && !str_starts_with($venvPath, '/')) {
        return e3dcInvalidInstallPaths('Venv-Pfad ist nicht absolut.');
    }

    return [
        'valid' => true,
        'error' => '',
        'install_user' => $installUser,
        'install_path' => $installRoot . '/',
        'home_dir' => $homeDir,
        'venv_path' => $venvPath,
    ];
}

function getInstallPath() {
    $paths = getInstallPaths();
    return $paths['install_path'];
}

/**
 * Ermittelt den korrekten Python-Interpreter (venv oder system).
 * Liest venv_name und venv_path aus e3dc_v4.json.
 */
function getPythonInterpreter() {
    $paths = getInstallPaths();
    $v4 = @json_decode(@file_get_contents('/var/www/html/data/e3dc_v4.json'), true);
    $v4 = is_array($v4) ? $v4 : [];
    $venvName = trim((string)($v4['venv_name'] ?? ''));

    // 1. Expliziter venv_path aus V4 Config
    if (!empty($v4['venv_path']) && file_exists($v4['venv_path'] . '/bin/python3')) {
        return $v4['venv_path'] . '/bin/python3';
    }

    // 2. Standard Pfad: home_dir / venv_name
    $venvPython = !empty($paths['valid']) && $venvName !== ''
        ? rtrim($paths['home_dir'], '/') . '/' . $venvName . '/bin/python3'
        : '';
    if ($venvPython !== '' && file_exists($venvPython) && is_executable($venvPython)) {
        return $venvPython;
    }

    // 3. Docker Fallback
    if (file_exists('/.dockerenv') && file_exists('/opt/venv/bin/python3')) {
        return '/opt/venv/bin/python3';
    }

    return '/usr/bin/python3';
}

/**
 * Löst einen explizit konfigurierten Python-Interpreter auf.
 * Keine PATH-Suche und kein aus POST-Daten zusammengesetzter Pfad.
 */
function e3dcGetTrustedPythonInterpreter() {
    $v4Path = '/var/www/html/data/e3dc_v4.json';
    $raw = is_readable($v4Path) ? @json_decode((string)@file_get_contents($v4Path), true) : null;
    $cfg = is_array($raw) ? $raw : [];
    if (isset($cfg['config']) && is_array($cfg['config'])) {
        $cfg = array_replace($cfg['config'], $cfg);
        unset($cfg['config']);
    }

    $candidates = [];
    $venvPath = trim((string)($cfg['venv_path'] ?? ''));
    if ($venvPath !== '' && str_starts_with($venvPath, '/')) {
        $candidates[] = rtrim($venvPath, '/') . '/bin/python3';
    }
    $homeDir = trim((string)($cfg['home_dir'] ?? ''));
    $venvName = trim((string)($cfg['venv_name'] ?? ''));
    if ($homeDir !== '' && $venvName !== '' && str_starts_with($homeDir, '/') && strpos($venvName, '/') === false) {
        $candidates[] = rtrim($homeDir, '/') . '/' . $venvName . '/bin/python3';
    }
    if (is_file('/.dockerenv')) {
        $candidates[] = '/opt/venv/bin/python3';
    }
    // Der Systeminterpreter ist ein fester, nicht durch Request-Daten steuerbarer
    // Kompatibilitätspfad für bestehende v5.3.2a-/v5.3.2b-Installationen ohne venv_path.
    $candidates[] = '/usr/bin/python3';

    foreach (array_values(array_unique($candidates)) as $candidate) {
        if (!str_starts_with($candidate, '/') || is_link($candidate) && @realpath($candidate) === false) continue;
        $resolved = @realpath($candidate);
        if ($resolved === false || !is_file($resolved) || !is_executable($candidate)) continue;
        return $candidate;
    }
    return null;
}

/**
 * Startet ein festes argv ohne Shell und liefert Exitcode, Timeout und Signal
 * getrennt zurück. stdout/stderr werden begrenzt, damit ein fehlerhafter
 * Prozess den PHP-Worker nicht unbegrenzt belegen kann.
 */
function e3dcRunArgvProcess(array $argv, $timeoutSeconds = 20.0, array $options = []) {
    $result = [
        'success' => false,
        'exit_code' => 127,
        'timed_out' => false,
        'signal' => 0,
        'stdout' => '',
        'stderr' => '',
        'error' => '',
        'duration_ms' => 0,
    ];
    if (empty($argv)) {
        $result['error'] = 'Leerer Prozessaufruf.';
        return $result;
    }
    $cleanArgv = [];
    foreach ($argv as $value) {
        if (!is_scalar($value) || strpos((string)$value, "\0") !== false) {
            $result['error'] = 'Ungültiges Prozessargument.';
            return $result;
        }
        $cleanArgv[] = (string)$value;
    }
    $executable = $cleanArgv[0];
    if (!str_starts_with($executable, '/') || !is_file($executable) || !is_executable($executable)) {
        $result['error'] = 'Interpreter oder Programm nicht verfügbar.';
        return $result;
    }
    $timeoutSeconds = max(0.1, min(300.0, (float)$timeoutSeconds));
    $maxOutput = max(1024, min(1024 * 1024, (int)($options['max_output_bytes'] ?? 65536)));
    $cwd = isset($options['cwd']) && is_string($options['cwd']) && is_dir($options['cwd'])
        ? $options['cwd']
        : null;
    $environment = null;
    if (isset($options['env']) && is_array($options['env'])) {
        $baseEnv = getenv();
        $environment = array_merge(is_array($baseEnv) ? $baseEnv : [], $options['env']);
    }
    $descriptors = [
        0 => ['pipe', 'r'],
        1 => ['pipe', 'w'],
        2 => ['pipe', 'w'],
    ];
    $pipes = [];
    $start = microtime(true);
    $process = @proc_open(
        $cleanArgv,
        $descriptors,
        $pipes,
        $cwd,
        $environment,
        ['bypass_shell' => true, 'suppress_errors' => true]
    );
    if (!is_resource($process)) {
        $result['error'] = 'Prozess konnte nicht gestartet werden.';
        $result['duration_ms'] = (int)round((microtime(true) - $start) * 1000);
        return $result;
    }
    @fclose($pipes[0]);
    @stream_set_blocking($pipes[1], false);
    @stream_set_blocking($pipes[2], false);
    $exitCode = null;
    $signal = 0;
    $wasSignaled = false;
    while (true) {
        foreach ([1 => 'stdout', 2 => 'stderr'] as $index => $key) {
            $chunk = @stream_get_contents($pipes[$index]);
            if ($chunk !== false && $chunk !== '' && strlen($result[$key]) < $maxOutput) {
                $result[$key] .= substr($chunk, 0, $maxOutput - strlen($result[$key]));
            }
        }
        $status = @proc_get_status($process);
        if (!is_array($status)) {
            $result['error'] = 'Prozessstatus nicht lesbar.';
            break;
        }
        if (!$status['running']) {
            $exitCode = isset($status['exitcode']) ? (int)$status['exitcode'] : null;
            $signal = isset($status['termsig']) ? (int)$status['termsig'] : 0;
            $wasSignaled = !empty($status['signaled']);
            break;
        }
        if ((microtime(true) - $start) >= $timeoutSeconds) {
            $result['timed_out'] = true;
            @proc_terminate($process, 15);
            $graceUntil = microtime(true) + 0.5;
            do {
                usleep(20000);
                $status = @proc_get_status($process);
            } while (is_array($status) && !empty($status['running']) && microtime(true) < $graceUntil);
            if (is_array($status) && !empty($status['running'])) {
                @proc_terminate($process, 9);
            }
            if (is_array($status)) {
                $signal = isset($status['termsig']) ? (int)$status['termsig'] : 15;
            }
            $exitCode = 124;
            break;
        }
        usleep(20000);
    }
    foreach ([1 => 'stdout', 2 => 'stderr'] as $index => $key) {
        $chunk = @stream_get_contents($pipes[$index]);
        if ($chunk !== false && $chunk !== '' && strlen($result[$key]) < $maxOutput) {
            $result[$key] .= substr($chunk, 0, $maxOutput - strlen($result[$key]));
        }
        @fclose($pipes[$index]);
    }
    $closeCode = @proc_close($process);
    if ($exitCode === null || $exitCode < 0) {
        $exitCode = is_int($closeCode) && $closeCode >= 0 ? $closeCode : 1;
    }
    if ($wasSignaled && $signal === 0 && is_int($closeCode) && $closeCode > 0 && $closeCode <= 64) {
        $signal = $closeCode;
        $exitCode = 128 + $signal;
    }
    // Einige proc_open-Implementierungen stellen ein Signal nur über den
    // konventionellen Exit-Status 128+Signal des argv-Kindprozesses bereit.
    if ($signal === 0 && $exitCode >= 129 && $exitCode <= 192) {
        $signal = $exitCode - 128;
    }
    $result['exit_code'] = (int)$exitCode;
    $result['signal'] = (int)$signal;
    $result['duration_ms'] = (int)round((microtime(true) - $start) * 1000);
    if ($result['timed_out']) {
        $result['error'] = 'Prozess-Timeout.';
    } elseif ($result['signal'] > 0) {
        $result['error'] = 'Prozess durch Signal beendet.';
    } elseif ($result['exit_code'] !== 0) {
        $result['error'] = 'Prozess meldete einen Fehler.';
    }
    $result['success'] = !$result['timed_out'] && $result['signal'] === 0 && $result['exit_code'] === 0;
    return $result;
}

/**
 * Erzeugt eine Seiten-URL im aktuellen Kontext (mobile.php oder index.php).
 *
 * @param string $seite Zielseite
 * @param array $params Zusätzliche Query-Parameter
 * @return string
 */
function getContextPageUrl($seite, $params = []) {
    $script = basename($_SERVER['PHP_SELF'] ?? 'index.php');
    $entrypoint = ($script === 'mobile.php') ? 'mobile.php' : 'index.php';

    $query = array_merge(['seite' => $seite], $params);
    return $entrypoint . '?' . http_build_query($query);
}

/**
 * Gibt Erfolgs- oder Info-Meldung aus
 */
function successMessage($message) {
    return '<div class="success-box" style="background:#2d3d2a; border-left:4px solid #27ae60; padding:15px; margin:15px 0; border-radius:4px; color:#27ae60; font-weight:bold;">'
           . htmlspecialchars($message) . '</div>';
}

// ==================== DATEIOPERATIONEN ====================

/**
 * Sichere Datei-Leseoperation mit Fehlerbehandlung
 *
 * @param string $path Dateipfad
 * @param bool $asArray true = array, false = string
 * @return array|string|false Dateiinhalt oder false bei Fehler
 */
function safeReadFile($path, $asArray = false) {
    $check = checkFileAccess($path, 'read');
    if ($check !== true) {
        return false;
    }

    if ($asArray) {
        return file($path, FILE_IGNORE_NEW_LINES) ?: false;
    }
    return file_get_contents($path) ?: false;
}

/**
 * Sichere Datei-Schreiboperation mit Fehlerbehandlung
 */
function safeWriteFile($path, $content, $flags = LOCK_EX) {
    $check = checkFileAccess($path, 'write');
    if ($check !== true) {
        return false;
    }

    return @file_put_contents($path, $content, $flags) !== false;
}

// ==================== VALIDIERUNG ====================

/**
 * Validiert einen Dateinamen gegen Path-Traversal-Attacken
 */
function validateFilename($filename) {
    // Nur alphanumerisch, Punkte, Unterstriche, Bindestriche
    if (!preg_match('/^[a-zA-Z0-9._\-]+$/', $filename)) {
        return false;
    }
    // basename() entfernt Pfade
    if (basename($filename) !== $filename) {
        return false;
    }
    return true;
}

/**
 * Sanitiert Benutzereingaben
 */
function sanitizeInput($input) {
    return htmlspecialchars(trim($input), ENT_QUOTES, 'UTF-8');
}

/**
 * Prüft auf erforderliche POST-Parameter
 */
function requirePostParams($required = []) {
    if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
        return true;
    }

    foreach ($required as $param) {
        if (!isset($_POST[$param]) || $_POST[$param] === '') {
            die(errorMessage("Erforderlicher Parameter fehlt", "Parameter: " . htmlspecialchars($param)));
        }
    }
    return true;
}

// ==================== KONFIGURATION ====================

/**
 * Liest eine reguläre Datei descriptor- und identitätsgebunden.
 *
 * Damit darf ein Symlink- oder Rename-Rennen weder Authentifizierung noch
 * Konfigurationsentscheidungen auf eine andere Datei umlenken.
 */
function e3dcReadRegularFileBound($path, $maxBytes = 1048576) {
    if (!is_string($path) || $path === '' || is_link($path)) return null;
    $pathStat = @lstat($path);
    if (!is_array($pathStat)) return null;
    $mode = (int)($pathStat['mode'] ?? 0);
    $size = (int)($pathStat['size'] ?? -1);
    if (($mode & 0170000) !== 0100000 || $size < 0 || $size > (int)$maxBytes) {
        return null;
    }
    $handle = @fopen($path, 'rb');
    if ($handle === false) return null;
    try {
        $opened = @fstat($handle);
        if (
            !is_array($opened)
            || (int)($opened['dev'] ?? -1) !== (int)($pathStat['dev'] ?? -2)
            || (int)($opened['ino'] ?? -1) !== (int)($pathStat['ino'] ?? -2)
            || (int)($opened['nlink'] ?? 0) !== 1
            || (int)($opened['size'] ?? -1) !== $size
        ) {
            return null;
        }
        $raw = @stream_get_contents($handle, (int)$maxBytes + 1);
        $after = @fstat($handle);
        $pathAfter = @lstat($path);
        if (
            !is_string($raw)
            || strlen($raw) !== $size
            || !is_array($after)
            || !is_array($pathAfter)
            || (int)($after['dev'] ?? -1) !== (int)($opened['dev'] ?? -2)
            || (int)($after['ino'] ?? -1) !== (int)($opened['ino'] ?? -2)
            || (int)($after['nlink'] ?? 0) !== 1
            || (int)($after['size'] ?? -1) !== $size
            || (int)($after['mtime'] ?? -1) !== (int)($opened['mtime'] ?? -2)
            || (int)($after['ctime'] ?? -1) !== (int)($opened['ctime'] ?? -2)
            || ((int)($pathAfter['mode'] ?? 0) & 0170000) !== 0100000
            || (int)($pathAfter['dev'] ?? -1) !== (int)($opened['dev'] ?? -2)
            || (int)($pathAfter['ino'] ?? -1) !== (int)($opened['ino'] ?? -2)
            || (int)($pathAfter['nlink'] ?? 0) !== 1
            || (int)($pathAfter['size'] ?? -1) !== $size
            || (int)($pathAfter['mtime'] ?? -1) !== (int)($opened['mtime'] ?? -2)
            || (int)($pathAfter['ctime'] ?? -1) !== (int)($opened['ctime'] ?? -2)
        ) {
            return null;
        }
        return $raw;
    } finally {
        @fclose($handle);
    }
}

function e3dcConfigRequestMemoGeneration($path) {
    clearstatcache(true, $path);
    $meta = @lstat($path);
    $epoch = (int)($GLOBALS['e3dc_config_request_memo_epoch'] ?? 0);
    if (!is_array($meta)) {
        return 'missing:' . $epoch;
    }
    return implode(':', [
        $epoch,
        (int)($meta['dev'] ?? -1),
        (int)($meta['ino'] ?? -1),
        (int)($meta['size'] ?? -1),
        (int)($meta['mtime'] ?? -1),
        (int)($meta['ctime'] ?? -1),
    ]);
}

function e3dcInvalidateConfigRequestMemo() {
    $GLOBALS['e3dc_config_request_memo_epoch'] =
        (int)($GLOBALS['e3dc_config_request_memo_epoch'] ?? 0) + 1;
}

function e3dcFirstFreshRegularFile($paths, $maxAgeS) {
    $now = microtime(true);
    $maxAge = max(0.0, (float)$maxAgeS);
    foreach ((array)$paths as $path) {
        $path = (string)$path;
        if ($path === '' || is_link($path)) continue;
        clearstatcache(true, $path);
        $meta = @lstat($path);
        if (
            !is_array($meta)
            || (((int)($meta['mode'] ?? 0)) & 0170000) !== 0100000
            || (int)($meta['nlink'] ?? 0) !== 1
            || !is_readable($path)
        ) {
            continue;
        }
        $mtime = (float)($meta['mtime'] ?? 0);
        $age = $now - $mtime;
        if ($mtime > 0.0 && $age >= 0.0 && $age <= $maxAge) {
            return $path;
        }
    }
    return null;
}

function e3dcWallboxSessionSourceGeneration($path) {
    $path = (string)$path;
    if ($path === '' || is_link($path)) return null;
    clearstatcache(true, $path);
    $meta = @lstat($path);
    if (
        !is_array($meta)
        || (((int)($meta['mode'] ?? 0)) & 0170000) !== 0100000
        || (int)($meta['nlink'] ?? 0) !== 1
        || (int)($meta['size'] ?? -1) < 0
        || !is_readable($path)
    ) {
        return null;
    }
    return [
        'key' => implode(':', [
            (int)($meta['dev'] ?? -1),
            (int)($meta['ino'] ?? -1),
            (int)($meta['size'] ?? -1),
            (int)($meta['mtime'] ?? -1),
            (int)($meta['ctime'] ?? -1),
        ]),
        'dev' => (int)($meta['dev'] ?? -1),
        'ino' => (int)($meta['ino'] ?? -1),
        'size' => (int)($meta['size'] ?? -1),
        'mtime' => (int)($meta['mtime'] ?? -1),
        'ctime' => (int)($meta['ctime'] ?? -1),
    ];
}

function e3dcFirstWallboxSessionHistoryFile($paths) {
    foreach ((array)$paths as $path) {
        if (is_array(e3dcWallboxSessionSourceGeneration($path))) {
            return (string)$path;
        }
    }
    return null;
}

function e3dcWallboxSessionEmptyAggregate($day) {
    return [
        'schema' => 1,
        'day' => (string)$day,
        'source_key' => '',
        'generation_key' => '',
        'total_kwh' => 0.0,
        'total_seconds' => 0,
        'session_count' => 0,
        'today_kwh' => ['1' => 0.0, '2' => 0.0],
        'recent_limit' => 500,
        'sessions' => [],
    ];
}

function e3dcWallboxSessionAggregateCacheValue($cacheFile, $sourceKey, $generationKey, $day) {
    $raw = e3dcReadRegularFileBound($cacheFile, 4 * 1024 * 1024);
    if (!is_string($raw)) return null;
    $cached = @json_decode($raw, true);
    if (
        !is_array($cached)
        || (int)($cached['schema'] ?? 0) !== 1
        || !isset($cached['source_key'], $cached['generation_key'], $cached['day'])
        || !hash_equals((string)$sourceKey, (string)$cached['source_key'])
        || !hash_equals((string)$generationKey, (string)$cached['generation_key'])
        || (string)$cached['day'] !== (string)$day
        || !is_numeric($cached['total_kwh'] ?? null)
        || !is_numeric($cached['total_seconds'] ?? null)
        || !is_numeric($cached['session_count'] ?? null)
        || (int)($cached['recent_limit'] ?? 0) !== 500
        || !isset($cached['today_kwh'])
        || !is_array($cached['today_kwh'])
        || !is_numeric($cached['today_kwh']['1'] ?? null)
        || !is_numeric($cached['today_kwh']['2'] ?? null)
        || !isset($cached['sessions'])
        || !is_array($cached['sessions'])
        || count($cached['sessions']) > 500
        || (float)$cached['total_kwh'] < 0.0
        || (int)$cached['total_seconds'] < 0
        || (int)$cached['session_count'] < count($cached['sessions'])
    ) {
        return null;
    }
    foreach ($cached['sessions'] as $session) {
        if (
            !is_array($session)
            || !is_numeric($session['tsStart'] ?? null)
            || !is_numeric($session['tsEnd'] ?? null)
            || !is_numeric($session['kwh'] ?? null)
            || (int)$session['tsEnd'] < (int)$session['tsStart']
            || (float)$session['kwh'] < 0.0
        ) {
            return null;
        }
    }
    return $cached;
}

function e3dcBuildWallboxSessionAggregate($path, $sourceKey, $generation, $day) {
    if (!is_array($generation)) return null;
    $handle = @fopen($path, 'rb');
    if ($handle === false) return null;
    $opened = @fstat($handle);
    clearstatcache(true, $path);
    $bound = @lstat($path);
    if (
        !is_array($opened)
        || !is_array($bound)
        || (((int)($opened['mode'] ?? 0)) & 0170000) !== 0100000
        || (((int)($bound['mode'] ?? 0)) & 0170000) !== 0100000
        || (int)($opened['nlink'] ?? 0) !== 1
        || (int)($bound['nlink'] ?? 0) !== 1
        || (int)($opened['dev'] ?? -1) !== (int)$generation['dev']
        || (int)($opened['ino'] ?? -1) !== (int)$generation['ino']
        || (int)($opened['size'] ?? -1) !== (int)$generation['size']
        || (int)($bound['dev'] ?? -1) !== (int)$generation['dev']
        || (int)($bound['ino'] ?? -1) !== (int)$generation['ino']
    ) {
        @fclose($handle);
        return null;
    }

    $totalKwh = 0.0;
    $totalSeconds = 0;
    $sessionCount = 0;
    $todayKwh = ['1' => 0.0, '2' => 0.0];
    $ring = [];
    $ringLimit = 500;

    while (($parts = @fgetcsv($handle, 1048576, ';', '"', '')) !== false) {
        if (!is_array($parts) || count($parts) < 5) continue;
        $marker = ltrim(trim((string)$parts[0]), "\xEF\xBB\xBF");
        if (strcasecmp($marker, 'Timestamp') === 0) continue;

        $startRaw = trim((string)$parts[1]);
        $endRaw = trim((string)$parts[2]);
        $kwhRaw = str_replace(',', '.', trim((string)$parts[3]));
        $wallbox = trim((string)$parts[4]);
        if (!is_numeric($kwhRaw)) continue;
        $kwh = max(0.0, (float)$kwhRaw);

        if (
            isset($todayKwh[$wallbox])
            && (strpos($marker, $day) === 0 || strpos($endRaw, $day) === 0)
        ) {
            $todayKwh[$wallbox] += $kwh;
        }

        $tsStart = strtotime($startRaw);
        $tsEnd = strtotime($endRaw);
        if ($tsStart === false || $tsEnd === false || $tsEnd < $tsStart) continue;

        $totalKwh += $kwh;
        $totalSeconds += $tsEnd - $tsStart;
        $ring[$sessionCount % $ringLimit] = [
            'tsStart' => $tsStart,
            'tsEnd' => $tsEnd,
            'kwh' => $kwh,
        ];
        $sessionCount++;
    }

    $after = @fstat($handle);
    @fclose($handle);
    $pathAfter = e3dcWallboxSessionSourceGeneration($path);
    if (
        !is_array($after)
        || !is_array($pathAfter)
        || (string)$pathAfter['key'] !== (string)$generation['key']
        || (int)($after['dev'] ?? -1) !== (int)$generation['dev']
        || (int)($after['ino'] ?? -1) !== (int)$generation['ino']
        || (int)($after['size'] ?? -1) !== (int)$generation['size']
        || (int)($after['mtime'] ?? -1) !== (int)$generation['mtime']
        || (int)($after['ctime'] ?? -1) !== (int)$generation['ctime']
    ) {
        return null;
    }

    $recentCount = min($sessionCount, $ringLimit);
    $chronological = [];
    $startIndex = $sessionCount > $ringLimit ? $sessionCount % $ringLimit : 0;
    for ($offset = 0; $offset < $recentCount; $offset++) {
        $index = ($startIndex + $offset) % $ringLimit;
        if (isset($ring[$index])) $chronological[] = $ring[$index];
    }

    return [
        'schema' => 1,
        'day' => (string)$day,
        'source_key' => (string)$sourceKey,
        'generation_key' => (string)$generation['key'],
        'total_kwh' => $totalKwh,
        'total_seconds' => $totalSeconds,
        'session_count' => $sessionCount,
        'today_kwh' => [
            '1' => round($todayKwh['1'], 3),
            '2' => round($todayKwh['2'], 3),
        ],
        'recent_limit' => $ringLimit,
        'sessions' => array_reverse($chronological),
    ];
}

function e3dcWriteWallboxSessionAggregateCache($cacheFile, $aggregate) {
    $dir = dirname((string)$cacheFile);
    if (!is_dir($dir) || !is_array($aggregate)) return false;
    $body = @json_encode($aggregate, JSON_UNESCAPED_SLASHES);
    if (!is_string($body)) return false;
    $tmp = @tempnam($dir, '.wb_sessions_aggregate_');
    if ($tmp === false) return false;
    $ok = @file_put_contents($tmp, $body, LOCK_EX) !== false;
    if ($ok) {
        @chmod($tmp, 0664);
        $ok = @rename($tmp, $cacheFile);
    }
    @unlink($tmp);
    return $ok;
}

function e3dcWallboxSessionCsvAggregate(
    $path,
    $cacheFile = '/var/www/html/ramdisk/wb_sessions_aggregate.json'
) {
    static $requestMemo = [];

    $day = date('Y-m-d');
    $empty = e3dcWallboxSessionEmptyAggregate($day);
    $generation = e3dcWallboxSessionSourceGeneration($path);
    if (!is_array($generation)) return $empty;
    $sourceKey = hash('sha256', (string)$path);
    $memoKey = $sourceKey . ':' . $generation['key'] . ':' . $day;
    if (isset($requestMemo[$memoKey]) && is_array($requestMemo[$memoKey])) {
        return $requestMemo[$memoKey];
    }

    $cached = e3dcWallboxSessionAggregateCacheValue(
        $cacheFile,
        $sourceKey,
        $generation['key'],
        $day
    );
    if (is_array($cached)) {
        $requestMemo = [$memoKey => $cached];
        return $cached;
    }

    $lockPath = $cacheFile . '.lock';
    $lockHandle = is_link($lockPath) ? false : @fopen($lockPath, 'c');
    if ($lockHandle === false && !is_link($lockPath)) {
        $lockHandle = @fopen($lockPath, 'r');
    }
    if (is_resource($lockHandle)) {
        clearstatcache(true, $lockPath);
        $openedLock = @fstat($lockHandle);
        $boundLock = @lstat($lockPath);
        if (
            !is_array($openedLock)
            || !is_array($boundLock)
            || (((int)($openedLock['mode'] ?? 0)) & 0170000) !== 0100000
            || (((int)($boundLock['mode'] ?? 0)) & 0170000) !== 0100000
            || (int)($openedLock['nlink'] ?? 0) !== 1
            || (int)($boundLock['nlink'] ?? 0) !== 1
            || (int)($openedLock['dev'] ?? -1) !== (int)($boundLock['dev'] ?? -2)
            || (int)($openedLock['ino'] ?? -1) !== (int)($boundLock['ino'] ?? -2)
        ) {
            @fclose($lockHandle);
            $lockHandle = false;
        }
    }
    $lockOwned = false;
    if (is_resource($lockHandle)) {
        @chmod($lockPath, 0664);
        $deadline = microtime(true) + 0.5;
        do {
            if (@flock($lockHandle, LOCK_EX | LOCK_NB)) {
                $lockOwned = true;
                break;
            }
            usleep(25000);
        } while (microtime(true) < $deadline);
    }

    if ($lockOwned) {
        // Ein anderer Prozess kann den exakt gebundenen Cache während unserer
        // begrenzten Wartezeit bereits erzeugt haben.
        $cached = e3dcWallboxSessionAggregateCacheValue(
            $cacheFile,
            $sourceKey,
            $generation['key'],
            $day
        );
        if (is_array($cached)) {
            @flock($lockHandle, LOCK_UN);
            @fclose($lockHandle);
            $requestMemo = [$memoKey => $cached];
            return $cached;
        }
    }

    $aggregate = e3dcBuildWallboxSessionAggregate(
        $path,
        $sourceKey,
        $generation,
        $day
    );
    if (!is_array($aggregate)) {
        if ($lockOwned) @flock($lockHandle, LOCK_UN);
        if (is_resource($lockHandle)) @fclose($lockHandle);
        return $empty;
    }
    if ($lockOwned) {
        e3dcWriteWallboxSessionAggregateCache($cacheFile, $aggregate);
        @flock($lockHandle, LOCK_UN);
    }
    if (is_resource($lockHandle)) @fclose($lockHandle);
    $requestMemo = [$memoKey => $aggregate];
    return $aggregate;
}

function e3dcRemoveConfigCacheFailClosed($cacheFile) {
    if (!file_exists($cacheFile) && !is_link($cacheFile)) return true;
    if (@unlink($cacheFile)) return true;
    error_log('E3DC-Konfigurationscache konnte nicht sicher entfernt werden.');
    return false;
}

function e3dcConfigCacheMetadataMatches($cacheFile, $expectedMode) {
    $meta = @lstat($cacheFile);
    if (!is_array($meta)) return false;
    if ((((int)$meta['mode']) & 0170000) !== 0100000) return false;
    if ((int)($meta['nlink'] ?? 0) !== 1) return false;
    if ((((int)$meta['mode']) & 0777) !== (int)$expectedMode) return false;
    if (function_exists('posix_geteuid') && (int)$meta['uid'] !== (int)posix_geteuid()) return false;
    if (function_exists('posix_getegid') && (int)$meta['gid'] !== (int)posix_getegid()) return false;
    return true;
}

function e3dcWriteConfigCacheSecurely($cacheFile, $cacheBody, $configData) {
    if (!is_string($cacheBody)) return e3dcRemoveConfigCacheFailClosed($cacheFile);
    $expectedMode = e3dcConfigSecretFileModeFromData($configData);
    if (
        e3dcConfigCacheMetadataMatches($cacheFile, $expectedMode)
        && @file_get_contents($cacheFile) === $cacheBody
    ) {
        return true;
    }

    $cacheDir = dirname($cacheFile);
    $tmpFile = @tempnam($cacheDir, '.e3dc_config_cache.');
    $ok = is_string($tmpFile) && dirname($tmpFile) === $cacheDir;
    if ($ok) {
        $written = @file_put_contents($tmpFile, $cacheBody, LOCK_EX);
        $ok = $written === strlen($cacheBody);
    }
    if ($ok) $ok = @chmod($tmpFile, $expectedMode);
    if ($ok) $ok = e3dcConfigCacheMetadataMatches($tmpFile, $expectedMode);
    if ($ok) $ok = @rename($tmpFile, $cacheFile);
    if ($ok) {
        $tmpFile = null;
        $ok = e3dcConfigCacheMetadataMatches($cacheFile, $expectedMode)
            && @file_get_contents($cacheFile) === $cacheBody;
    }
    if (is_string($tmpFile) && (file_exists($tmpFile) || is_link($tmpFile))) {
        @unlink($tmpFile);
    }
    if ($ok) return true;

    error_log('E3DC-Konfigurationscache konnte nicht atomar und geschützt veröffentlicht werden.');
    return e3dcRemoveConfigCacheFailClosed($cacheFile);
}

/**
 * Lädt die E3DC-Konfiguration aus e3dc_v4.json (Single Source of Truth).
 * e3dc.config.txt wird nicht mehr gelesen.
 */
function loadE3dcConfig($basePath = null) {
    static $requestMemo = [];

    $v4Path = '/var/www/html/data/e3dc_v4.json';
    $cacheFile = '/var/www/html/ramdisk/e3dc_config_cache.json';
    $memoGeneration = e3dcConfigRequestMemoGeneration($v4Path);
    if (isset($requestMemo[$memoGeneration]) && is_array($requestMemo[$memoGeneration])) {
        return $requestMemo[$memoGeneration];
    }

    // Pro Request wird ausschließlich die aktuelle Dateigeneration gehalten.
    // Ein erfolgreicher Schreibpfad erhöht zusätzlich den lokalen Memo-Epoch.
    $requestMemo = [];
    $v4Raw = e3dcReadRegularFileBound($v4Path, 1048576);
    if (!is_string($v4Raw)) {
        e3dcRemoveConfigCacheFailClosed($cacheFile);
        $result = ['error' => errorMessage('Konfiguration fehlt', 'e3dc_v4.json nicht gefunden unter ' . $v4Path), 'config' => []];
        $requestMemo[$memoGeneration] = $result;
        return $result;
    }
    if (strlen($v4Raw) < 2) {
        e3dcRemoveConfigCacheFailClosed($cacheFile);
        $result = ['error' => errorMessage('Konfigurationsfehler', 'e3dc_v4.json besitzt keine zulässige Größe.'), 'config' => []];
        $requestMemo[$memoGeneration] = $result;
        return $result;
    }

    // Der Cache ist ein Laufzeitspiegel für andere Module, aber keine
    // Authentifizierungsautorität. Deshalb wird die gebundene Quelldatei bei
    // jedem PHP-Aufruf erneut geparst.
    $v4_mtime = @filemtime($v4Path);
    if ($v4_mtime === false) {
        e3dcRemoveConfigCacheFailClosed($cacheFile);
        $result = ['error' => errorMessage('Konfigurationsfehler', 'e3dc_v4.json kann nicht sicher geprüft werden.'), 'config' => []];
        $requestMemo[$memoGeneration] = $result;
        return $result;
    }
    $v4Data = @json_decode($v4Raw, true);
    if (!is_array($v4Data)) {
        e3dcRemoveConfigCacheFailClosed($cacheFile);
        $result = ['error' => errorMessage('Konfigurationsfehler', 'e3dc_v4.json ist kein gültiges JSON.'), 'config' => []];
        $requestMemo[$memoGeneration] = $result;
        return $result;
    }

    $config = [];
    foreach ($v4Data as $k => $v) {
        if (!is_array($v)) {
            $config[strtolower($k)] = $v;
        }
    }

    // Der Spiegel liegt in tmpfs und wird nur bei einer echten Änderung
    // erneuert. Er bleibt reine Projektion und niemals Auth-Autorität.
    $cacheBody = json_encode(['mtime' => (string)$v4_mtime, 'config' => $config]);
    if (!e3dcWriteConfigCacheSecurely($cacheFile, $cacheBody, $v4Data)) {
        $result = ['error' => errorMessage('Konfigurationsschutz', 'Der lokale Konfigurationscache konnte nicht sicher veröffentlicht oder entfernt werden.'), 'config' => []];
        $requestMemo[$memoGeneration] = $result;
        return $result;
    }

    $result = ['error' => null, 'config' => $config];
    $stableRaw = e3dcReadRegularFileBound($v4Path, 1048576);
    $stableGeneration = e3dcConfigRequestMemoGeneration($v4Path);
    if ($stableRaw === $v4Raw && $stableGeneration === $memoGeneration) {
        $requestMemo[$memoGeneration] = $result;
    } elseif (!e3dcRemoveConfigCacheFailClosed($cacheFile)) {
        return [
            'error' => errorMessage('Konfigurationsschutz', 'Ein Cache einer überholten Konfigurationsgeneration konnte nicht sicher entfernt werden.'),
            'config' => [],
        ];
    }
    return $result;
}

function e3dcConfigSecretProtectionModeFromData($data) {
    if (!is_array($data)) return 'standard';
    $raw = strtolower(str_replace(['-', ' '], '_', trim((string)($data['config_secret_protection_mode'] ?? 'standard'))));
    return in_array($raw, ['compat', 'compatible', 'compatibility', 'legacy', 'world_readable', '664'], true)
        ? 'compatibility'
        : 'standard';
}

function e3dcConfigSecretFileModeFromData($data) {
    return e3dcConfigSecretProtectionModeFromData($data) === 'compatibility' ? 0664 : 0660;
}

function e3dcConfigSecretDirModeFromData($data) {
    return e3dcConfigSecretProtectionModeFromData($data) === 'compatibility' ? 02775 : 02770;
}

function e3dcJsonAtomicFileMode($path, $json) {
    if (basename((string)$path) !== 'e3dc_v4.json') return 0664;
    $decoded = @json_decode((string)$json, true);
    return e3dcConfigSecretFileModeFromData(is_array($decoded) ? $decoded : []);
}

function e3dcWriteJsonPreservingOwner($path, $json, $fileMode) {
    if (basename((string)$path) !== 'e3dc_v4.json') return false;
    if (!file_exists($path) || !is_writable($path)) return false;
    $payload = $json . "\n";
    $fh = @fopen($path, 'c+');
    if ($fh === false) return false;
    $ok = false;
    if (@flock($fh, LOCK_EX)) {
        $original = @stream_get_contents($fh);
        if ($original === false) $original = null;
        @rewind($fh);
        $bytes = @fwrite($fh, $payload);
        if ($bytes === strlen($payload) && @ftruncate($fh, strlen($payload)) && @fflush($fh)) {
            $ok = true;
        } elseif ($original !== null) {
            @rewind($fh);
            @fwrite($fh, $original);
            @ftruncate($fh, strlen($original));
            @fflush($fh);
        }
        @flock($fh, LOCK_UN);
    }
    @fclose($fh);
    if ($ok) {
        @chgrp($path, 'www-data');
        @chmod($path, $fileMode);
    }
    return $ok;
}

function e3dcWriteJsonAtomic($path, $json) {
    $dir = dirname($path);
    if (!is_dir($dir)) return false;
    $fileMode = e3dcJsonAtomicFileMode($path, $json);
    if (e3dcWriteJsonPreservingOwner($path, $json, $fileMode)) {
        if (basename((string)$path) === 'e3dc_v4.json') {
            e3dcInvalidateConfigRequestMemo();
        }
        return true;
    }
    $tmpFile = tempnam($dir, '.e3dc_v4_');
    if ($tmpFile === false) return false;
    $ok = @file_put_contents($tmpFile, $json . "\n", LOCK_EX) !== false;
    if ($ok) {
        @chgrp($tmpFile, 'www-data');
        @chmod($tmpFile, $fileMode);
        $ok = @rename($tmpFile, $path);
    }
    if (!$ok) {
        @unlink($tmpFile);
        return false;
    }
    @chgrp($path, 'www-data');
    @chmod($path, $fileMode);
    if (basename((string)$path) === 'e3dc_v4.json') {
        e3dcInvalidateConfigRequestMemo();
    }
    return true;
}

function e3dcReadExistingJsonOrFalse($path) {
    if (!file_exists($path)) return [];
    $raw = @file_get_contents($path);
    if ($raw === false) return false;
    $decoded = @json_decode($raw, true);
    return is_array($decoded) ? $decoded : false;
}

/**
 * Speichert V4-Konfigurationswerte atomar in e3dc_v4.json.
 * Legacy-TXT-Dateien werden hier bewusst nicht mehr beschrieben.
 */
function saveE3dcConfigValues($updates) {
    if (!is_array($updates) || empty($updates)) {
        return false;
    }

    $v4Path = '/var/www/html/data/e3dc_v4.json';
    $cacheFile = '/var/www/html/ramdisk/e3dc_config_cache.json';
    $data = e3dcReadExistingJsonOrFalse($v4Path);
    if ($data === false) return false;

    foreach ($updates as $key => $value) {
        $key = strtolower(trim((string)$key));
        if (!preg_match('/^[a-z0-9_]+$/i', $key)) {
            continue;
        }
        $data[$key] = is_string($value) ? trim($value) : $value;
    }

    $json = json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    if ($json === false) {
        return false;
    }

    $ok = e3dcWriteJsonAtomic($v4Path, $json);

    if ($ok && file_exists($cacheFile)) {
        @unlink($cacheFile);
    }
    return $ok;
}

function saveE3dcConfigValue($key, $value) {
    return saveE3dcConfigValues([$key => $value]);
}

function loadE3dcRawConfigData() {
    $v4Path = '/var/www/html/data/e3dc_v4.json';
    if (!file_exists($v4Path)) return [];
    $decoded = @json_decode(@file_get_contents($v4Path), true);
    return is_array($decoded) ? $decoded : [];
}

function saveE3dcRawConfigData($data) {
    if (!is_array($data)) return false;
    $v4Path = '/var/www/html/data/e3dc_v4.json';
    $cacheFile = '/var/www/html/ramdisk/e3dc_config_cache.json';
    $json = json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    if ($json === false) return false;

    $ok = e3dcWriteJsonAtomic($v4Path, $json);

    if ($ok && file_exists($cacheFile)) @unlink($cacheFile);
    return $ok;
}

function energyFlowDefaultColors() {
    return [
        'pv' => '#ffc107',
        'external_pv' => '#22c55e',
        'grid' => '#6c757d',
        'grid_import' => '#ef4444',
        'grid_export' => '#2ecc71',
        'home' => '#0dcaf0',
        'battery' => '#198754',
        'battery_charge' => '#2ecc71',
        'wallbox' => '#2ecc71',
        'wallbox2' => '#34d399',
        'heatpump' => '#f97316',
        'heater' => '#fd7e14',
        'climate' => '#38bdf8',
        'generation' => '#22c55e',
        'consumption' => '#0dcaf0',
        'center' => '#0d6efd'
    ];
}

function energyFlowDefaultLabels() {
    return [
        'pv' => 'E3DC-PV', 'external_pv' => 'Zusatz-WR', 'grid' => 'Netz',
        'battery' => 'Speicher', 'home' => 'Haus', 'wallbox' => 'Wallbox 1',
        'wallbox2' => 'Wallbox 2', 'heatpump' => 'Wärmepumpe', 'heater' => 'Heizstab',
        'climate' => 'Klima', 'generation' => 'Erzeugung', 'consumption' => 'Verbrauch'
    ];
}

function sanitizeEnergyFlowLabel($value) {
    if (!is_scalar($value)) return '';
    $value = trim(strip_tags((string)$value));
    $value = preg_replace('/[\x00-\x1F\x7F]/u', '', $value) ?? '';
    if ($value === '') return '';
    return function_exists('mb_substr') ? mb_substr($value, 0, 32, 'UTF-8') : substr($value, 0, 32);
}

function sanitizeEnergyFlowLabels($labels) {
    $clean = [];
    if (!is_array($labels)) return $clean;
    foreach (array_keys(energyFlowDefaultLabels()) as $key) {
        if (!array_key_exists($key, $labels)) continue;
        $alias = sanitizeEnergyFlowLabel($labels[$key]);
        if ($alias !== '') $clean[$key] = $alias;
    }
    return $clean;
}

function normalizeEnergyFlowColor($value, $fallback = '#6c757d') {
    $value = strtolower(trim((string)$value));
    if (preg_match('/^#[0-9a-f]{6}$/', $value)) return $value;
    if (preg_match('/^#([0-9a-f])([0-9a-f])([0-9a-f])$/', $value, $m)) {
        return '#' . $m[1] . $m[1] . $m[2] . $m[2] . $m[3] . $m[3];
    }
    return $fallback;
}

function normalizeEnergyFlowPercent($value, $fallback) {
    if (is_string($value)) $value = str_replace(',', '.', trim($value));
    if (!is_numeric($value)) return (float)$fallback;
    return max(4.0, min(96.0, round((float)$value, 2)));
}

function sanitizeEnergyFlowNodes($nodes) {
    $allowed = ['pv', 'external_pv', 'grid', 'battery', 'home', 'wallbox', 'wallbox2', 'heatpump', 'heater', 'climate', 'generation', 'consumption', 'center'];
    $clean = [];
    if (!is_array($nodes)) return $clean;
    foreach ($allowed as $key) {
        if (!isset($nodes[$key]) || !is_array($nodes[$key])) continue;
        $clean[$key] = [
            'x' => normalizeEnergyFlowPercent($nodes[$key]['x'] ?? null, 50),
            'y' => normalizeEnergyFlowPercent($nodes[$key]['y'] ?? null, 50)
        ];
    }
    return $clean;
}

function normalizeEnergyFlowUiConfig($raw) {
    if (!is_array($raw)) $raw = [];
    $stored = (isset($raw['ui_energy_flow']) && is_array($raw['ui_energy_flow'])) ? $raw['ui_energy_flow'] : [];
    $defaults = energyFlowDefaultColors();
    $colors = $defaults;
    if (isset($stored['colors']) && is_array($stored['colors'])) {
        foreach ($defaults as $key => $fallback) {
            if (isset($stored['colors'][$key])) {
                $colors[$key] = normalizeEnergyFlowColor($stored['colors'][$key], $fallback);
            }
        }
    }
    $state = [
        'desktop' => ['nodes' => sanitizeEnergyFlowNodes($stored['desktop']['nodes'] ?? [])],
        'mobile' => ['nodes' => sanitizeEnergyFlowNodes($stored['mobile']['nodes'] ?? [])],
        'colors' => $colors,
        'labels' => sanitizeEnergyFlowLabels($stored['labels'] ?? [])
    ];
    $canonical = function($value) {
        $json = json_encode($value, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
        return hash('sha256', $json === false ? '' : $json);
    };
    $state['revisions'] = [
        'desktop' => $canonical($state['desktop']['nodes']),
        'mobile' => $canonical($state['mobile']['nodes']),
        'appearance' => $canonical(['colors' => $state['colors'], 'labels' => $state['labels']])
    ];
    $state['revision'] = $canonical([
        'desktop' => $state['desktop'],
        'mobile' => $state['mobile'],
        'colors' => $state['colors'],
        'labels' => $state['labels']
    ]);
    return $state;
}

function getEnergyFlowUiConfig() {
    return normalizeEnergyFlowUiConfig(loadE3dcRawConfigData());
}

function saveEnergyFlowUiPatchLocked($layout, $nodes, $colorPatch, $labelPatch, $baseRevisions = [], $v4Path = null, $cacheFile = null) {
    $layout = strtolower(trim((string)$layout));
    if (!in_array($layout, ['desktop', 'mobile'], true)) {
        return ['success' => false, 'status' => 'invalid_layout'];
    }
    $nodePatch = sanitizeEnergyFlowNodes($nodes);
    if (!is_array($nodes) || count($nodePatch) === 0) {
        return ['success' => false, 'status' => 'invalid_nodes'];
    }
    $expectedLayoutRevision = is_array($baseRevisions) ? strtolower(trim((string)($baseRevisions[$layout] ?? ''))) : '';
    $appearanceChanges = (is_array($colorPatch) && count($colorPatch) > 0)
        || (is_array($labelPatch) && count($labelPatch) > 0);
    $expectedAppearanceRevision = is_array($baseRevisions) ? strtolower(trim((string)($baseRevisions['appearance'] ?? ''))) : '';
    if (!preg_match('/^[0-9a-f]{64}$/', $expectedLayoutRevision)
        || ($appearanceChanges && !preg_match('/^[0-9a-f]{64}$/', $expectedAppearanceRevision))) {
        return ['success' => false, 'status' => 'invalid_base_revision'];
    }
    $v4Path = is_string($v4Path) && $v4Path !== '' ? $v4Path : '/var/www/html/data/e3dc_v4.json';
    $cacheFile = is_string($cacheFile) && $cacheFile !== '' ? $cacheFile : '/var/www/html/ramdisk/e3dc_config_cache.json';
    if (!file_exists($v4Path) || !is_writable($v4Path)) {
        return ['success' => false, 'status' => 'config_not_writable'];
    }

    $fh = @fopen($v4Path, 'c+');
    if ($fh === false) return ['success' => false, 'status' => 'config_open_failed'];
    $result = ['success' => false, 'status' => 'config_lock_failed'];
    if (@flock($fh, LOCK_EX)) {
        @rewind($fh);
        $original = @stream_get_contents($fh);
        $decoded = is_string($original) ? @json_decode($original, true) : null;
        if (!is_array($decoded)) {
            $result = ['success' => false, 'status' => 'config_invalid_json'];
        } else {
            $before = normalizeEnergyFlowUiConfig($decoded);
            $layoutConflict = !e3dcWebAuthHashEquals($before['revisions'][$layout] ?? '', $expectedLayoutRevision);
            $appearanceConflict = $appearanceChanges
                && !e3dcWebAuthHashEquals($before['revisions']['appearance'] ?? '', $expectedAppearanceRevision);

            if ($layoutConflict || $appearanceConflict) {
                $result = [
                    'success' => false,
                    'status' => 'revision_conflict',
                    'conflicts' => array_values(array_filter([
                        $layoutConflict ? $layout : null,
                        $appearanceConflict ? 'appearance' : null
                    ])),
                    'ui_energy_flow' => $before,
                    'revision' => $before['revision'] ?? '',
                    'revisions' => $before['revisions'] ?? []
                ];
            } else {
                $ui = (isset($decoded['ui_energy_flow']) && is_array($decoded['ui_energy_flow']))
                    ? $decoded['ui_energy_flow']
                    : [];
                $ui[$layout] = is_array($ui[$layout] ?? null) ? $ui[$layout] : [];
                $storedNodes = (isset($ui[$layout]['nodes']) && is_array($ui[$layout]['nodes']))
                    ? $ui[$layout]['nodes']
                    : [];
                foreach ($nodePatch as $key => $position) {
                    $storedNode = is_array($storedNodes[$key] ?? null) ? $storedNodes[$key] : [];
                    $storedNode['x'] = $position['x'];
                    $storedNode['y'] = $position['y'];
                    $storedNodes[$key] = $storedNode;
                }
                $ui[$layout]['nodes'] = $storedNodes;

                $defaults = energyFlowDefaultColors();
                $storedColors = (isset($ui['colors']) && is_array($ui['colors'])) ? $ui['colors'] : [];
                if (is_array($colorPatch)) {
                    foreach ($colorPatch as $key => $value) {
                        if (!array_key_exists($key, $defaults)) continue;
                        $storedColors[$key] = normalizeEnergyFlowColor($value, $defaults[$key]);
                    }
                }
                $ui['colors'] = $storedColors;

                $storedLabels = (isset($ui['labels']) && is_array($ui['labels'])) ? $ui['labels'] : [];
                if (is_array($labelPatch)) {
                    foreach ($labelPatch as $key => $value) {
                        if (!array_key_exists($key, energyFlowDefaultLabels())) continue;
                        $alias = sanitizeEnergyFlowLabel($value);
                        if ($alias === '') unset($storedLabels[$key]);
                        else $storedLabels[$key] = $alias;
                    }
                }
                $ui['labels'] = $storedLabels;
                $decoded['ui_energy_flow'] = $ui;

                $json = json_encode($decoded, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
                if ($json === false) {
                    $result = ['success' => false, 'status' => 'config_encode_failed'];
                } else {
                    $payload = $json . "\n";
                    @rewind($fh);
                    $bytes = @fwrite($fh, $payload);
                    $written = $bytes === strlen($payload)
                        && @ftruncate($fh, strlen($payload))
                        && @fflush($fh);
                    if (!$written) {
                        @rewind($fh);
                        if (is_string($original)) {
                            @fwrite($fh, $original);
                            @ftruncate($fh, strlen($original));
                            @fflush($fh);
                        }
                        $result = ['success' => false, 'status' => 'config_write_failed'];
                    } else {
                        $after = normalizeEnergyFlowUiConfig($decoded);
                        $result = [
                            'success' => true,
                            'status' => 'saved',
                            'layout' => $layout,
                            'ui_energy_flow' => $after,
                            'revision' => $after['revision'] ?? '',
                            'revisions' => $after['revisions'] ?? []
                        ];
                    }
                }
            }
        }
        @flock($fh, LOCK_UN);
    }
    @fclose($fh);
    if (!empty($result['success'])) {
        @chgrp($v4Path, 'www-data');
        @chmod($v4Path, e3dcConfigSecretFileModeFromData($decoded ?? []));
        if (file_exists($cacheFile)) @unlink($cacheFile);
        if (basename((string)$v4Path) === 'e3dc_v4.json') {
            e3dcInvalidateConfigRequestMemo();
        }
    }
    return $result;
}

function handleEnergyFlowLayout() {
    $payload = null;
    if (($_POST['action'] ?? '') === 'save_energy_flow_layout') {
        $payload = $_POST;
    } else {
        $rawBody = @file_get_contents('php://input');
        $decoded = @json_decode($rawBody, true);
        if (is_array($decoded) && ($decoded['action'] ?? '') === 'save_energy_flow_layout') {
            $payload = $decoded;
        }
    }
    if ($payload === null) return;

    e3dcRequirePostMutation(true);
    header('Content-Type: application/json; charset=utf-8');

    $layout = strtolower(trim((string)($payload['layout'] ?? '')));
    if (!in_array($layout, ['desktop', 'mobile'], true)) {
        http_response_code(400);
        echo json_encode(['success' => false, 'error' => 'ungültiges_layout']);
        exit;
    }

    $schemaVersion = trim((string)($payload['schema_version'] ?? ''));
    $colorPatch = isset($payload['colors_patch']) && is_array($payload['colors_patch'])
        ? $payload['colors_patch']
        : [];
    $labelPatch = isset($payload['labels_patch']) && is_array($payload['labels_patch'])
        ? $payload['labels_patch']
        : [];
    if ($schemaVersion !== 'energy_flow_layout_patch_v2') {
        http_response_code(409);
        echo json_encode([
            'success' => false,
            'status' => 'client_refresh_required',
            'error' => 'Seite neu laden, bevor das Layout gespeichert wird.'
        ]);
        exit;
    }

    $result = saveEnergyFlowUiPatchLocked(
        $layout,
        $payload['nodes'] ?? [],
        $colorPatch,
        $labelPatch,
        $payload['base_revisions'] ?? []
    );
    if (empty($result['success'])) {
        $status = (string)($result['status'] ?? '');
        http_response_code(in_array($status, ['revision_conflict', 'invalid_base_revision'], true) ? 409 : 500);
        $result['error'] = $status === 'revision_conflict'
            ? 'Das Layout wurde zwischenzeitlich geändert. Bitte Seite neu laden.'
            : ($status === 'invalid_base_revision'
                ? 'Seite neu laden, bevor das Layout gespeichert wird.'
                : 'Layout konnte nicht gespeichert werden.');
    }
    echo json_encode($result, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    exit;
}

function cfgBool($value, $default = false) {
    if ($value === null || $value === '') return $default;
    $v = strtolower(trim((string)$value));
    if (in_array($v, ['1', 'true', 'yes', 'on'], true)) return true;
    if (in_array($v, ['0', 'false', 'no', 'off'], true)) return false;
    return $default;
}

function e3dcPayloadContextValid($payload) {
    if (!is_array($payload)) return false;
    if (!array_key_exists('context_valid', $payload)) return true;
    $value = $payload['context_valid'];
    if (is_bool($value)) return $value;
    return !in_array(strtolower(trim((string)$value)), ['0', 'false', 'no', 'off', 'invalid'], true);
}

function cfgHasAddress($value) {
    $v = trim((string)($value ?? ''));
    $vLower = strtolower($v);
    return $v !== '' && !in_array($vLower, ['0', '0.0.0.0', 'none', 'null', 'false', 'off', 'disabled'], true);
}

function readExternalPvTopologyEvidence($path = '/var/www/html/data/external_pv_topology.json') {
    $unknown = [
        'topology_present' => false,
        'valid' => false,
        'source' => 'none',
        'evidence_state' => 'unknown',
        'reason' => 'not_confirmed',
    ];
    if (is_link($path)) {
        return array_merge($unknown, ['evidence_state' => 'invalid', 'reason' => 'state_not_regular']);
    }
    $metadata = @lstat($path);
    if ($metadata === false) {
        return file_exists($path)
            ? array_merge($unknown, ['evidence_state' => 'invalid', 'reason' => 'state_metadata_unavailable'])
            : $unknown;
    }
    if (($metadata['mode'] & 0170000) !== 0100000 || ($metadata['mode'] & 0777) !== 0664) {
        return array_merge($unknown, ['evidence_state' => 'invalid', 'reason' => 'state_mode_or_type_invalid']);
    }
    $raw = @file_get_contents($path);
    if (!is_string($raw) || $raw === '' || strlen($raw) > 4096) {
        return array_merge($unknown, ['evidence_state' => 'invalid', 'reason' => 'state_untrusted']);
    }
    $payload = json_decode($raw, true);
    $expectedKeys = [
        'schema_version', 'topology_present', 'valid', 'source', 'evidence_state',
        'confirmation_samples', 'minimum_power_w', 'confirmed_at',
    ];
    if (!is_array($payload)) {
        return array_merge($unknown, ['evidence_state' => 'invalid', 'reason' => 'state_untrusted']);
    }
    $actualKeys = array_keys($payload);
    sort($actualKeys);
    $sortedExpected = $expectedKeys;
    sort($sortedExpected);
    if ($actualKeys !== $sortedExpected) {
        return array_merge($unknown, ['evidence_state' => 'invalid', 'reason' => 'schema_fields_invalid']);
    }
    foreach ($expectedKeys as $key) {
        if (substr_count($raw, '"' . $key . '"') !== 1) {
            return array_merge($unknown, ['evidence_state' => 'invalid', 'reason' => 'duplicate_or_missing_field']);
        }
    }
    $confirmedAt = $payload['confirmed_at'] ?? null;
    $samples = $payload['confirmation_samples'] ?? null;
    $minimumPower = $payload['minimum_power_w'] ?? null;
    $valid = ($payload['schema_version'] ?? null) === 'external_pv_topology_v1'
        && ($payload['topology_present'] ?? null) === true
        && ($payload['valid'] ?? null) === true
        && ($payload['source'] ?? null) === 'e3dc_add_power'
        && ($payload['evidence_state'] ?? null) === 'confirmed'
        && is_int($samples) && $samples >= 3
        && (is_int($minimumPower) || is_float($minimumPower)) && is_finite((float)$minimumPower) && (float)$minimumPower >= 100.0
        && is_int($confirmedAt) && $confirmedAt > 0;
    if (!$valid) {
        return array_merge($unknown, ['evidence_state' => 'invalid', 'reason' => 'state_contract_invalid']);
    }
    return [
        'topology_present' => true,
        'valid' => true,
        'source' => 'e3dc_add_power',
        'evidence_state' => 'confirmed',
        'reason' => 'multi_sample_confirmed',
        'confirmation_samples' => $samples,
        'confirmed_at' => $confirmedAt,
    ];
}

function normalizeWallboxTypeConfig($type) {
    $type = strtolower(trim((string)$type));
    $aliases = [
        'goe' => 'go-e',
        'openwb-pro' => 'openwb_pro',
        'openwbpro' => 'openwb_pro',
        'e3dc_multi_connect' => 'e3dc_multi',
        'e3dc-multi' => 'e3dc_multi',
        'e3dc multi' => 'e3dc_multi',
        'e3dc_easy' => 'e3dc',
        'e3dc_legacy' => 'e3dc',
        'native' => 'e3dc',
        'off' => 'none',
        'disabled' => 'none',
        'deaktiviert' => 'none',
        'keine' => 'none',
        'false' => 'none',
        'no' => 'none',
        '0' => 'none',
        '-1' => 'none',
    ];
    return $aliases[$type] ?? $type;
}

function isConfiguredWallboxTypeConfig($type) {
    $type = normalizeWallboxTypeConfig($type);
    return $type !== '' && $type !== 'none';
}

function hasWallbox1Config($cfg) {
    if (!is_array($cfg)) return false;

    // Ein vorhandener Typ-Key ist die aktuelle, ausdrückliche Nutzerwahl.
    // Restaurierte Legacy-IP-/Topic-Werte dürfen "none" nicht überstimmen.
    if (array_key_exists('wb_native_type', $cfg)) {
        return isConfiguredWallboxTypeConfig($cfg['wb_native_type']);
    }

    if (cfgHasAddress($cfg['wb_topic'] ?? '')
        || cfgHasAddress($cfg['wb_ip'] ?? '')
        || cfgHasAddress($cfg['shelly_wb_ip'] ?? '')
        || cfgHasAddress($cfg['wb1_topic'] ?? '')
        || cfgHasAddress($cfg['wb1_topic_prefix'] ?? '')) {
        return true;
    }

    if (cfgHasAddress($cfg['wb_native_ip'] ?? '')) {
        return true;
    }

    if (array_key_exists('wallbox', $cfg)) {
        $legacy = strtolower(trim((string)$cfg['wallbox']));
        if (in_array($legacy, ['-1', 'false', 'no', 'off', 'none', 'disabled'], true)) return false;
        if (in_array($legacy, ['1', 'true', 'yes', 'on'], true)) return true;
        if (is_numeric($legacy)) return (int)$legacy >= 0;
    }

    if (cfgBool($cfg['wb_native_enable'] ?? null, false)) return true;

    // Ohne positiven Capability-/Konfigurationsbeleg gibt es keine Wallbox-Kachel.
    return false;
}

function hasWallbox2Config($cfg) {
    if (!is_array($cfg)) return false;

    // Ein leerer oder abgeschalteter aktueller Typ bedeutet ausdrücklich:
    // keine zweite Wallbox, auch wenn alte Adresswerte noch vorhanden sind.
    if (array_key_exists('wb_native_type2', $cfg)) {
        return isConfiguredWallboxTypeConfig($cfg['wb_native_type2']);
    }

    if (cfgHasAddress($cfg['wb2_topic'] ?? '')
        || cfgHasAddress($cfg['wb2_ip'] ?? '')
        || cfgHasAddress($cfg['shelly_wb2_ip'] ?? '')
        || cfgHasAddress($cfg['wb2_topic_prefix'] ?? '')) {
        return true;
    }

    if (cfgHasAddress($cfg['wb_native_ip2'] ?? '')) {
        return true;
    }

    // Bei aktivem nativen Multi-Wallbox Manager in der Ramdisk
    if (is_file('/var/www/html/ramdisk/wallbox_native.json')) {
        $raw = @file_get_contents('/var/www/html/ramdisk/wallbox_native.json');
        if ($raw) {
            $nw = @json_decode($raw, true);
            if (is_array($nw)) {
                if (isset($nw['wb_multi_contract']['slots']) && is_array($nw['wb_multi_contract']['slots']) && count($nw['wb_multi_contract']['slots']) > 1) {
                    return true;
                }
                if (isset($nw['wb_details']) && is_array($nw['wb_details'])) {
                    foreach ($nw['wb_details'] as $det) {
                        if (is_array($det) && (int)($det['id'] ?? 0) === 2) return true;
                    }
                }
                if (preg_match('/Multi\s*\((\d+)/i', (string)($nw['wb_type'] ?? ''), $m) && (int)$m[1] > 1) {
                    return true;
                }
            }
        }
    }

    return false;
}

function hasAnyWallboxConfig($cfg) {
    return hasWallbox1Config($cfg) || hasWallbox2Config($cfg);
}

function hasNativeWallboxStatusConfig($cfg) {
    if (!is_array($cfg)) return false;
    return cfgBool($cfg['wb_native_enable'] ?? null, false) && hasAnyWallboxConfig($cfg);
}

/**
 * Entfernt Live-/Cachewerte für ausdrücklich nicht konfigurierte Wallboxen.
 * Das Konfigurationsflag ist der Vertrag; alte Ramdisk-, Session- oder
 * Zählerwerte dürfen einen deaktivierten Slot nicht wieder sichtbar machen.
 */
function e3dcApplyWallboxPresenceProjection(&$data, $wb1Configured, $wb2Configured) {
    if (!is_array($data)) $data = [];
    $wb1Configured = (bool)$wb1Configured;
    $wb2Configured = (bool)$wb2Configured;

    // Falls ein nativer Multi-Wallbox Vertrag aktiv ist, wird WB2 nie gekappt
    if (isset($data['wb_multi_contract']['slots']) && is_array($data['wb_multi_contract']['slots']) && count($data['wb_multi_contract']['slots']) > 1) {
        $wb2Configured = true;
    }

    if (isset($data['wb_details']) && is_array($data['wb_details'])) {
        $data['wb_details'] = array_values(array_filter(
            $data['wb_details'],
            static function($detail) use ($wb1Configured, $wb2Configured) {
                if (!is_array($detail)) return false;
                $id = (int)($detail['id'] ?? 0);
                if ($id === 1) return $wb1Configured;
                if ($id === 2) return $wb2Configured;
                return true;
            }
        ));
    }

    if (!$wb1Configured) {
        $wb1Keys = [
            'wb', 'wb_p1', 'wb_p2', 'wb_p3', 'wb_kva', 'wb_power_factor',
            'wb_set_amp', 'wb_cap_amp', 'wb_status_amp',
            'wb_offered_current_raw', 'wb_current_step_amp',
            'wb_fractional_current_supported', 'wb_plug', 'wb_locked',
            'wb_charging', 'wb_status_valid', 'wb_status_source',
            'wb_status_reason', 'wb_runtime_manual_pause', 'wb_state_text',
            'wb_state_reason', 'wb_source', 'wb_home_relation',
            'wb_home_correction_source', 'wb_suppressed_power_w',
            'wb_session_kwh', 'wb_daily_kwh', 'wb_daily_raw_kwh',
            'wb_daily_base_kwh', 'wb_display_held', 'wb_chargemode',
            'wb_chargepoint_name', 'wb_fault_text', 'wb_phases',
            'wb_phases_actual', 'wb_phases_target', 'wb_can_switch_phases',
            'wb_phase_switch_capability', 'wb_phase_switch_source',
            'wb_api_surface', 'wb_control_status', 'wb_control_label',
            'wb_control_detail', 'wb_control_level', 'wb_last_command_ok',
            'wb_last_command_amp', 'wb_last_heartbeat_ok',
            'wb_configured_role', 'wb_detected_role', 'wb_effective_role',
            'wb_role_mismatch', 'wb_command_failure_count',
            'wb_command_failure_limit', 'wb_command_blocked', 'wb_evse_a',
            'wb_cp_id', 'wb_native_ip', 'wb_soc', 'wb_soc_source',
            'wb_soc_source_ts', 'wb_soc_rule_confirmed', 'wb_range',
            'wb_charged_range', 'wb_charge_profile_name',
            'wb_charge_profile_source', 'wb_car_name', 'wb_car_id',
            'wb_vehicle_id', 'wb_rfid_tag', 'wb_rfid_timestamp',
            'wb_live_car_name', 'wb_live_car_id', 'wb_live_vehicle_id',
            'wb_live_rfid_tag', 'wb_vehicle_identity_current',
            'wb_stable_vehicle_identity_current', 'wb_display_car_name',
            'wb_display_car_source', 'wb_pro_serial', 'wb_pro_temp_c',
            'python_wb1_total_kwh', 'python_wb1_session_kwh',
            'e_wb', 'e_wb_source', 'e_wb_source_priority',
        ];
        foreach ($wb1Keys as $key) unset($data[$key]);
        $data['wb'] = 0.0;
        $data['wb_configured'] = false;
        $data['is_external_wb'] = false;
        $data['connected'] = false;
        $data['charging_active'] = false;
        $data['detected_phases'] = 0;
    }

    if (!$wb2Configured) {
        foreach (array_keys($data) as $key) {
            if (
                $key === 'wb2'
                || (
                    strpos($key, 'wb2_') === 0
                    && !in_array($key, ['wb2_configured', 'wb2_native_type', 'wb2_manual_pause'], true)
                )
                || strpos($key, 'python_wb2_') === 0
                || $key === 'e_wb2'
                || strpos($key, 'e_wb2_') === 0
            ) {
                unset($data[$key]);
            }
        }
        $data['wb2'] = 0.0;
        $data['wb2_configured'] = false;
        $data['is_external_wb2'] = false;
    }

    $activeWallboxId = (int)($data['active_wb_id'] ?? 0);
    if (
        ($activeWallboxId === 1 && !$wb1Configured)
        || ($activeWallboxId === 2 && !$wb2Configured)
    ) {
        unset($data['active_wb_id'], $data['active_wb_phases']);
    }
}

function getHeatpumpTypeConfig($cfg) {
    if (!is_array($cfg)) return -1;
    if (array_key_exists('wp_type', $cfg) && trim((string)$cfg['wp_type']) !== '') {
        return (int)$cfg['wp_type'];
    }
    return cfgBool($cfg['luxtronik'] ?? null, false) ? 0 : -1;
}

function isHeatpumpEnabledConfig($cfg) {
    if (!is_array($cfg)) return false;
    $wpType = getHeatpumpTypeConfig($cfg);
    if ($wpType < 0 || $wpType === 2) return false;

    if ($wpType === 0) {
        return cfgBool($cfg['luxtronik'] ?? null, false)
            || (!array_key_exists('luxtronik', $cfg) && cfgHasAddress($cfg['luxtronik_ip'] ?? ''));
    }
    if ($wpType === 1) {
        return cfgBool($cfg['luxtronik'] ?? null, false)
            || (!array_key_exists('luxtronik', $cfg) && cfgHasAddress($cfg['idm_ip'] ?? ''));
    }
    if ($wpType === 3) {
        return cfgHasAddress($cfg['shelly_3em_ip'] ?? '') || cfgBool($cfg['luxtronik'] ?? null, false);
    }
    if ($wpType === 4) {
        return cfgBool($cfg['luxtronik'] ?? null, false)
            || (!array_key_exists('luxtronik', $cfg) && cfgHasAddress($cfg['stiebel_isg_ip'] ?? ''));
    }
    if ($wpType === 5) {
        return cfgBool($cfg['luxtronik'] ?? null, false)
            || (!array_key_exists('luxtronik', $cfg) && cfgHasAddress($cfg['dimplex_ip'] ?? ''));
    }
    return false;
}

/**
 * Wandelt den Zeitstempel eines Live-Vertrags in Unix-Sekunden um.
 * Unterstützt Unix-Sekunden, Unix-Millisekunden und ISO-Datumsangaben.
 */
function e3dcParsePayloadTimestamp($value) {
    if ($value === null || $value === '') return null;

    if (is_numeric($value)) {
        $numeric = (float)$value;
        if (!is_finite($numeric)) return null;
        if (abs($numeric) >= 100000000000.0) {
            $numeric /= 1000.0;
        }
        $timestamp = (int)floor($numeric);
        return $timestamp > 0 ? $timestamp : null;
    }

    if (!is_string($value)) return null;
    $text = trim($value);
    if ($text === '') return null;
    $timestamp = strtotime($text);
    return ($timestamp !== false && $timestamp > 0) ? (int)$timestamp : null;
}

/**
 * Wählt den jüngsten frischen, erfolgreichen Live-Vertrag des erwarteten
 * Herstellers. Datei- und Payload-Alter müssen beide passen; dadurch macht
 * ein frisch kopierter alter Vertrag keine veralteten Messwerte wieder gültig.
 */
function e3dcSelectFreshManufacturerPayload(
    $candidateFiles,
    $expectedManufacturer,
    $maxAgeSeconds = 150,
    $nowTs = null,
    $maxFutureSkewSeconds = 30
) {
    $result = [
        'status' => 'missing',
        'payload' => null,
        'path' => '',
        'source' => '',
        'age_s' => null,
        'error' => 'Keine Kandidatendatei vorhanden',
    ];
    if (!is_array($candidateFiles)) {
        $result['status'] = 'error';
        $result['error'] = 'Kandidatendateien müssen als Liste übergeben werden';
        return $result;
    }

    $manufacturerNeedle = strtolower(trim((string)$expectedManufacturer));
    if ($manufacturerNeedle === '') {
        $result['status'] = 'error';
        $result['error'] = 'Erwarteter Hersteller fehlt';
        return $result;
    }

    $now = ($nowTs === null) ? time() : (int)$nowTs;
    if ($now <= 0) $now = time();
    $maxAge = max(1, (int)$maxAgeSeconds);
    $maxFutureSkew = max(0, (int)$maxFutureSkewSeconds);
    $valid = [];
    $failures = [];

    foreach ($candidateFiles as $candidateFile) {
        if (!is_string($candidateFile) || trim($candidateFile) === '' || !is_file($candidateFile)) {
            continue;
        }

        $path = $candidateFile;
        $name = basename($path);
        $mtime = @filemtime($path);
        if ($mtime === false || (int)$mtime <= 0) {
            $failures[] = [
                'status' => 'error',
                'payload' => null,
                'path' => $path,
                'source' => '',
                'age_s' => null,
                'error' => $name . ': Änderungszeit konnte nicht gelesen werden',
                '_sort_age_s' => PHP_INT_MAX,
                '_mtime' => 0,
            ];
            continue;
        }

        $mtime = (int)$mtime;
        $fileAge = max(0, $now - $mtime);
        $raw = @file_get_contents($path);
        $payload = ($raw === false || trim($raw) === '') ? null : @json_decode($raw, true);
        if (!is_array($payload)) {
            $failures[] = [
                'status' => 'error',
                'payload' => null,
                'path' => $path,
                'source' => '',
                'age_s' => null,
                'error' => $name . ' enthält kein gültiges JSON',
                '_sort_age_s' => $fileAge,
                '_mtime' => $mtime,
            ];
            continue;
        }

        $payloadTimestamp = e3dcParsePayloadTimestamp($payload['ts'] ?? null);
        if ($payloadTimestamp === null) {
            $failures[] = [
                'status' => 'error',
                'payload' => null,
                'path' => $path,
                'source' => '',
                'age_s' => null,
                'error' => $name . ': Payload-Zeitstempel fehlt oder ist ungültig',
                '_sort_age_s' => $fileAge,
                '_mtime' => $mtime,
            ];
            continue;
        }

        $payloadAge = max(0, $now - $payloadTimestamp);
        $effectiveAge = max($fileAge, $payloadAge);
        $payloadData = (isset($payload['data']) && is_array($payload['data']))
            ? $payload['data']
            : $payload;
        $sourceParts = [
            trim((string)($payload['source'] ?? '')),
            trim((string)($payloadData['Quelle'] ?? '')),
            trim((string)($payloadData['Hersteller'] ?? '')),
        ];
        $sourceLabel = '';
        foreach ($sourceParts as $sourcePart) {
            if ($sourcePart !== '') {
                $sourceLabel = $sourcePart;
                break;
            }
        }
        $sourceIdentity = strtolower(trim(implode(' ', $sourceParts)));
        $candidate = [
            'status' => 'live',
            'payload' => $payload,
            'path' => $path,
            'source' => $sourceLabel,
            'age_s' => $effectiveAge,
            'error' => '',
            '_sort_age_s' => $effectiveAge,
            '_mtime' => $mtime,
        ];

        if ($mtime > ($now + $maxFutureSkew) || $payloadTimestamp > ($now + $maxFutureSkew)) {
            $candidate['status'] = 'future';
            $candidate['payload'] = null;
            $candidate['error'] = $name . ': Zeitstempel liegt unplausibel in der Zukunft';
            $failures[] = $candidate;
            continue;
        }
        if (($payload['success'] ?? null) !== true) {
            $candidate['status'] = 'error';
            $candidate['payload'] = null;
            $payloadError = trim((string)($payload['error'] ?? ''));
            $candidate['error'] = $name . ': ' . ($payloadError !== ''
                ? $payloadError
                : 'Live-Dienst meldet keinen erfolgreichen Abruf');
            $failures[] = $candidate;
            continue;
        }
        if ($sourceIdentity === '' || strpos($sourceIdentity, $manufacturerNeedle) === false) {
            $candidate['status'] = 'invalid_source';
            $candidate['payload'] = null;
            $candidate['error'] = $name . ' ist keine bestätigte ' . $expectedManufacturer . '-Quelle';
            $failures[] = $candidate;
            continue;
        }
        if ($effectiveAge > $maxAge) {
            $candidate['status'] = 'stale';
            $candidate['payload'] = null;
            $candidate['error'] = $name . ' ist wirksam ' . $effectiveAge . ' s alt';
            $failures[] = $candidate;
            continue;
        }

        $valid[] = $candidate;
    }

    $sortNewest = static function($left, $right) {
        $ageOrder = ((int)$left['_sort_age_s']) <=> ((int)$right['_sort_age_s']);
        if ($ageOrder !== 0) return $ageOrder;
        return ((int)$right['_mtime']) <=> ((int)$left['_mtime']);
    };
    $stripInternal = static function($candidate) {
        unset($candidate['_sort_age_s'], $candidate['_mtime']);
        return $candidate;
    };

    if ($valid) {
        usort($valid, $sortNewest);
        return $stripInternal($valid[0]);
    }
    if ($failures) {
        usort($failures, $sortNewest);
        return $stripInternal($failures[0]);
    }
    return $result;
}

function hasFreshMqttHeatpumpInbound($cfg = null, $maxAgeSeconds = 180) {
    if (is_array($cfg) && !cfgBool($cfg['mqtt_ha_inbound_enable'] ?? '1', true)) {
        return false;
    }

    $file = '/var/www/html/ramdisk/mqtt_ha_inbound.json';
    if (!file_exists($file)) return false;
    $mtime = @filemtime($file);
    if (!$mtime || (time() - $mtime) >= max(30, (int)$maxAgeSeconds)) return false;

    $raw = @file_get_contents($file);
    if ($raw === false || $raw === '') return false;
    $data = @json_decode($raw, true);
    if (!is_array($data)) return false;
    if (!e3dcPayloadContextValid($data)) return false;

    $sources = (isset($data['sources']) && is_array($data['sources'])) ? $data['sources'] : [];
    $heatpump = (isset($sources['heatpump']) && is_array($sources['heatpump'])) ? $sources['heatpump'] : [];
    if (!$heatpump) return false;

    if (isset($heatpump['ts']) && (time() - (int)$heatpump['ts']) >= max(30, (int)$maxAgeSeconds)) {
        return false;
    }

    foreach (['power_w', 'electric_w', 'ww_temp', 'ww_target_temp', 'flow_temp', 'return_temp', 'outside_temp', 'heat_kw', 'mode', 'boost_active'] as $key) {
        if (array_key_exists($key, $heatpump) && $heatpump[$key] !== '' && $heatpump[$key] !== null) {
            return true;
        }
    }

    return false;
}

function isHeaterEnabledConfig($cfg) {
    if (!is_array($cfg)) return false;
    $wpType = getHeatpumpTypeConfig($cfg);
    return $wpType === 2
        || cfgBool($cfg['heizstab'] ?? null, false)
        || cfgHasAddress($cfg['heizstab_ip'] ?? '')
        || cfgHasAddress($cfg['shelly_heiz_ip'] ?? '');
}

// ==================== LOGGING ====================

/**
 * Optionales Logging (für Debugging)
 */
function debugLog($message, $data = null) {
    if (!defined('DEBUG_MODE') || !DEBUG_MODE) {
        return;
    }

    $logFile = '/var/www/html/logs/debug.log';
    $timestamp = date('Y-m-d H:i:s');
    $logMessage = "[$timestamp] " . $message;

    if ($data !== null) {
        $logMessage .= " | " . json_encode($data);
    }

    @error_log($logMessage . "\n", 3, $logFile);
}

// ==================== HTML UTILITIES ====================

/**
 * Formatiert einen Datetime-String
 */
function formatDateTime($timestamp, $format = 'd.m.Y H:i') {
    if (is_numeric($timestamp)) {
        return date($format, $timestamp);
    }
    return htmlspecialchars($timestamp);
}

/**
 * Erstellt ein sicheres Button-HTML-Element
 */
function createButton($label, $url = '', $class = 'form-button', $onclick = '') {
    if ($url) {
        return '<a href="' . htmlspecialchars($url) . '" class="' . $class . '">'
               . htmlspecialchars($label) . '</a>';
    }
    return '<button type="button" class="' . $class . '" onclick="' . htmlspecialchars($onclick) . '">'
           . htmlspecialchars($label) . '</button>';
}

/**
 * Liest den Update-Status aus dem Cache (für PHP-Rendering).
 */
function getUpdateStatusFromCache() {
    $cacheFile = '/var/www/html/ramdisk/e3dc_update_status.json';
    if (file_exists($cacheFile)) {
        $content = @file_get_contents($cacheFile);
        if ($content) {
            $data = json_decode($content, true);
            if (is_array($data) && isset($data['success']) && $data['success'] && isset($data['missing'])) {
                return (int)$data['missing'];
            }
        }
    }
    return 0;
}

function e3dcNormalizeReleaseTag($value) {
    $tag = trim((string)$value);
    if ($tag === '') return null;
    if (!preg_match('/^v?\d+\.\d+\.\d+[A-Za-z0-9._-]*$/', $tag)) return null;
    return (strpos($tag, 'v') === 0) ? $tag : ('v' . $tag);
}

function e3dcReleaseVersionValue($value) {
    $value = ltrim(trim((string)$value), 'vV');
    return $value;
}

function e3dcReleaseVersionParts($value) {
    $value = e3dcReleaseVersionValue($value);
    if (!preg_match('/^(\d+)\.(\d+)\.(\d+)([A-Za-z0-9._-]*)$/', $value, $matches)) {
        return null;
    }
    return [
        (int)$matches[1],
        (int)$matches[2],
        (int)$matches[3],
        strtolower(trim((string)$matches[4], '.-_')),
    ];
}

function e3dcCompareReleaseVersions($left, $right) {
    $a = e3dcReleaseVersionParts($left);
    $b = e3dcReleaseVersionParts($right);
    if (!$a || !$b) {
        return version_compare(e3dcReleaseVersionValue($left), e3dcReleaseVersionValue($right));
    }
    for ($i = 0; $i < 3; $i++) {
        if ($a[$i] === $b[$i]) continue;
        return ($a[$i] < $b[$i]) ? -1 : 1;
    }
    if ($a[3] === $b[3]) return 0;
    if ($a[3] === '') return -1;
    if ($b[3] === '') return 1;
    return strnatcasecmp($a[3], $b[3]);
}

function e3dcStableUpdateCheck() {
    $current = readInstalledVersion();
    if ($current === '') {
        return [
            'success' => false,
            'missing' => 0,
            'error' => 'Die installierte VERSION ist nicht lesbar.',
        ];
    }

    $latestUrl = 'https://github.com/A9xxx/Install-E3DC-Control/releases/latest';
    $releasePrefix = 'https://github.com/A9xxx/Install-E3DC-Control/releases/tag/';
    $curl = '/usr/bin/curl';
    if (!is_file($curl) || !is_executable($curl)) {
        return [
            'success' => false,
            'missing' => 0,
            'error' => 'curl fehlt; der Stable-Release kann nicht geprüft werden.',
        ];
    }
    $request = e3dcRunArgvProcess(
        [
            $curl,
            '-q',
            '--fail',
            '--silent',
            '--show-error',
            '--location',
            '--proto',
            '=https',
            '--tlsv1.2',
            '--output',
            '/dev/null',
            '--write-out',
            '%{url_effective}',
            $latestUrl,
        ],
        25.0,
        ['max_output_bytes' => 4096]
    );
    $effectiveUrl = trim((string)($request['stdout'] ?? ''));
    if (empty($request['ok']) || strpos($effectiveUrl, $releasePrefix) !== 0) {
        $detail = trim((string)($request['stderr'] ?? ''));
        return [
            'success' => false,
            'missing' => 0,
            'error' => 'Der aktuelle GitHub Stable-Release ist nicht erreichbar.'
                . ($detail !== '' ? ' ' . $detail : ''),
        ];
    }

    $targetTag = trim(substr($effectiveUrl, strlen($releasePrefix)), '/');
    $normalizedTag = e3dcNormalizeReleaseTag($targetTag);
    if ($normalizedTag === null || $normalizedTag !== $targetTag) {
        return [
            'success' => false,
            'missing' => 0,
            'error' => 'GitHub lieferte keinen eindeutigen Stable-Release-Tag.',
        ];
    }
    $target = e3dcReleaseVersionValue($targetTag);
    $comparison = e3dcCompareReleaseVersions($current, $target);
    return [
        'success' => true,
        'missing' => $comparison < 0 ? 1 : 0,
        'missing_exact' => true,
        'same_release' => $comparison === 0,
        'ahead' => $comparison > 0,
        'current_version' => $current,
        'target_version' => $target,
        'target_tag' => $targetTag,
        'upstream' => 'github_latest_stable_release',
    ];
}

function e3dcReadUpdatePolicy() {
    $policyFiles = ['/var/www/html/UPDATE_POLICY.json'];
    foreach (getFooterInstallRootCandidates() as $root) {
        $policyFiles[] = rtrim($root, '/') . '/UPDATE_POLICY.json';
    }
    foreach ($policyFiles as $file) {
        if (!is_readable($file)) continue;
        $policy = json_decode((string)@file_get_contents($file), true);
        if (is_array($policy)) return $policy;
    }
    return [];
}

function e3dcDockerHostUpdateCommandText() {
    return implode("\n", [
        '(',
        '  set -euo pipefail',
        '  if [ -f ./docker_compose_update.py ]; then',
        '    E3DC_DOCKER_HELPER=./docker_compose_update.py',
        '  elif [ -f ./Installer/docker_compose_update.py ]; then',
        '    E3DC_DOCKER_HELPER=./Installer/docker_compose_update.py',
        '  else',
        '    echo "docker_compose_update.py fehlt; zuerst den aktuellen Release-Verwaltungsbaum bereitstellen." >&2',
        '    exit 2',
        '  fi',
        '  sudo python3 "$E3DC_DOCKER_HELPER" --compose-dir . --sudo',
        '  sudo docker compose logs --tail=80 e3dc-control',
        ')',
    ]);
}

function e3dcDockerHostUpdateMessage() {
    return "Docker-Installation erkannt. Der Web-Updater führt im Container bewusst keinen Release-Wechsel aus.\n"
         . "Bitte auf dem Docker-Host in das Verzeichnis Deiner vorhandenen Compose-Konfiguration wechseln und dort ausführen:\n\n"
         . e3dcDockerHostUpdateCommandText();
}

function e3dcDockerReleaseCommandText($tag) {
    $tag = e3dcNormalizeReleaseTag($tag);
    if (!$tag) return '';
    return implode("\n", [
        '(',
        'set -euo pipefail',
        'TAG=' . $tag,
        'cd "${E3DC_DOCKER_PATH:?E3DC_DOCKER_PATH auf den Compose-Pfad setzen}"',
        'BACKUP="e3dc-data-$(date +%Y%m%d-%H%M%S).tgz"',
        'sudo docker compose exec -T e3dc-control tar czf - -C /var/www/html/data . > "$BACKUP"',
        'test -s "$BACKUP"',
        'if [ -f ./docker_compose_update.py ]; then E3DC_DOCKER_HELPER=./docker_compose_update.py;',
        'elif [ -f ./Installer/docker_compose_update.py ]; then E3DC_DOCKER_HELPER=./Installer/docker_compose_update.py;',
        'else echo "docker_compose_update.py fehlt; aktuellen Release-Verwaltungsbaum bereitstellen." >&2; exit 2; fi',
        'HELPER_ARGS=(--compose-dir . --sudo --image-tag "$TAG")',
        'if [ "$TAG" = "v5.3.2b" ]; then',
        '  HELPER_ARGS+=(--legacy-no-healthcheck-version 5.3.2b)',
        'fi',
        'sudo python3 "$E3DC_DOCKER_HELPER" "${HELPER_ARGS[@]}"',
        'sudo docker compose logs --tail=80 e3dc-control',
        ')',
        '# Für einen dauerhaften Pin E3DC_IMAGE_TAG=' . $tag . ' in einer vorhandenen .env ergänzen.',
    ]);
}

function e3dcBuildReleaseRollbackOptions($dockerEnvironment = null) {
    $policy = e3dcReadUpdatePolicy();
    $isDocker = is_bool($dockerEnvironment) ? $dockerEnvironment : e3dcIsDockerEnvironment();
    $currentVersion = readInstalledVersion();
    if ($currentVersion === '' && !empty($policy['version'])) {
        $currentVersion = trim((string)$policy['version']);
    }
    $currentComparable = e3dcReleaseVersionValue($currentVersion);
    $stableTag = e3dcNormalizeReleaseTag($policy['stable_release'] ?? ($policy['version'] ?? ''));

    $rawReleases = $policy['rollback_releases'] ?? [];
    if (!is_array($rawReleases)) $rawReleases = [];
    $releases = [];
    $seen = [];
    foreach ($rawReleases as $entry) {
        if (!is_array($entry)) continue;
        $tag = e3dcNormalizeReleaseTag($entry['tag'] ?? ($entry['version'] ?? ''));
        if (!$tag || isset($seen[$tag])) continue;
        $bareMetalSupported = (($entry['bare_metal_supported'] ?? null) === true);
        $dockerSupported = (($entry['docker_supported'] ?? null) === true);
        if (($isDocker && !$dockerSupported) || (!$isDocker && !$bareMetalSupported)) continue;
        $seen[$tag] = true;
        $version = e3dcReleaseVersionValue($entry['version'] ?? $tag);
        $isStable = !empty($entry['stable']) || ($stableTag && $tag === $stableTag);
        $versionDelta = ($currentComparable !== '') ? e3dcCompareReleaseVersions($version, $currentComparable) : 0;
        $isCurrent = ($currentComparable !== '' && $versionDelta === 0);
        $isDowngrade = ($currentComparable !== '' && $versionDelta < 0);
        $image = trim((string)($entry['docker_image'] ?? ('ghcr.io/a9xxx/install-e3dc-control:' . $tag)));
        $releases[] = [
            'version' => $version,
            'tag' => $tag,
            'label' => trim((string)($entry['label'] ?? ($tag . ($isStable ? ' Stable' : '')))),
            'release_date' => trim((string)($entry['release_date'] ?? '')),
            'stable' => $isStable,
            'current' => $isCurrent,
            'downgrade' => $isDowngrade,
            'bare_metal_supported' => $bareMetalSupported,
            'docker_supported' => $dockerSupported,
            'docker_image' => $image,
            'notes' => trim((string)($entry['notes'] ?? '')),
            'docker_commands' => $dockerSupported ? e3dcDockerReleaseCommandText($tag) : '',
            'bare_metal_summary' => $bareMetalSupported
                ? "Backup, Dienststopp, git fetch --tags, Checkout auf $tag, Rechte-Reparatur und Gesundheitstest."
                : '',
        ];
    }

    usort($releases, function($a, $b) {
        return e3dcCompareReleaseVersions($b['version'], $a['version']);
    });

    return [
        'success' => true,
        'docker' => $isDocker,
        'current_version' => $currentVersion,
        'stable_release' => $stableTag,
        'empty_message' => $isDocker
            ? 'Für Docker ist keine validierte Rückfallversion hinterlegt.'
            : 'Für Bare Metal ist derzeit kein sicher finalisierbarer Programm-Rückfall freigegeben. Verifizierte Datei-Backups bleiben davon unberührt.',
        'releases' => $releases,
    ];
}

function e3dcFindInstallerMainAndWrapper() {
    $paths = getInstallPaths();
    if (empty($paths['valid'])) return null;
    $candidates = [rtrim($paths['install_path'], '/') . '/installer_main.py'];
    foreach ($candidates as $candidate) {
        if (file_exists($candidate)) {
            $installerMain = realpath($candidate);
            $repoDir = dirname($installerMain);
            return [
                'installer_main' => $installerMain,
                'repo_dir' => $repoDir,
                'wrapper' => $repoDir . '/Installer/installer_wrapper.sh',
            ];
        }
    }
    return null;
}

/**
 * Der frühere gemeinsame sudo-Einstieg für Installer, Reparatur und Rückfall
 * ist zu breit und bleibt deaktiviert. Das reguläre Self-Update besitzt einen
 * getrennten argumentlosen root-eigenen Launcher und nutzt dieses Gate nicht.
 */
function e3dcPrivilegedInstallerWebActionsEnabled() {
    return false;
}

function e3dcPrivilegedInstallerWebBlockMessage($operation) {
    $label = trim((string)$operation);
    if ($label === '') $label = 'Diese Aktion';
    return $label . ' ist im Web aus Sicherheitsgründen deaktiviert. '
         . 'Bitte dafür weiterhin eine administrative Konsole verwenden.';
}

function e3dcResolveGitObjectId($repoDir, $objectSpec) {
    $repoDir = (string)$repoDir;
    $objectSpec = (string)$objectSpec;
    if ($repoDir === '' || $objectSpec === '') return null;
    $git = '/usr/bin/git';
    if (!is_file($git) || !is_executable($git)) return null;
    $process = e3dcRunArgvProcess(
        [
            $git,
            '-c',
            'safe.directory=' . $repoDir,
            '-C',
            $repoDir,
            'rev-parse',
            '--verify',
            $objectSpec,
        ],
        10.0,
        ['max_output_bytes' => 4096]
    );
    $value = strtolower(trim((string)($process['stdout'] ?? '')));
    return (!empty($process['success'])
        && (int)($process['exit_code'] ?? 1) === 0
        && preg_match('/^[0-9a-f]{40}$/', $value))
        ? $value
        : null;
}

function e3dcInspectInstallerWrapper($wrapper) {
    $wrapper = (string)$wrapper;
    $result = [
        'ok' => false,
        'path' => $wrapper,
        'status' => 'missing',
        'repairable' => true,
    ];
    if ($wrapper === '') {
        return $result;
    }

    clearstatcache(true, $wrapper);
    if (is_link($wrapper)) {
        $result['status'] = 'symlink';
        $result['repairable'] = false;
        return $result;
    }
    $metadata = @lstat($wrapper);
    if ($metadata === false) {
        return $result;
    }
    $result['nlink'] = (int)($metadata['nlink'] ?? 0);
    $result['mode'] = (int)($metadata['mode'] ?? 0);
    $result['dev'] = (int)($metadata['dev'] ?? -1);
    $result['ino'] = (int)($metadata['ino'] ?? -1);
    $result['uid'] = (int)($metadata['uid'] ?? -1);
    $result['gid'] = (int)($metadata['gid'] ?? -1);
    $result['size'] = (int)($metadata['size'] ?? -1);
    $result['mtime'] = (int)($metadata['mtime'] ?? -1);
    $result['ctime'] = (int)($metadata['ctime'] ?? -1);
    if (($result['mode'] & 0170000) !== 0100000) {
        $result['status'] = 'not_regular';
        $result['repairable'] = false;
        return $result;
    }
    if ($result['nlink'] !== 1) {
        $result['status'] = 'hardlink';
        $result['repairable'] = false;
        return $result;
    }

    $repoDir = realpath(dirname(dirname($wrapper)));
    $wrapperParent = realpath(dirname($wrapper));
    if ($repoDir === false || $wrapperParent === false
        || $wrapperParent !== $repoDir . '/Installer'
        || basename($wrapper) !== 'installer_wrapper.sh') {
        $result['status'] = 'unbound_path';
        $result['repairable'] = false;
        return $result;
    }
    $head = e3dcResolveGitObjectId($repoDir, 'HEAD^{commit}');
    $blob = $head ? e3dcResolveGitObjectId($repoDir, $head . ':Installer/installer_wrapper.sh') : null;
    if ($head === null || $blob === null) {
        $result['status'] = 'head_unbound';
        $result['repairable'] = false;
        return $result;
    }
    $result['head'] = $head;

    $handle = @fopen($wrapper, 'rb');
    if ($handle === false) {
        $result['status'] = 'not_readable';
        $result['repairable'] = false;
        return $result;
    }
    $openedBefore = @fstat($handle);
    $actual = (string)@stream_get_contents($handle);
    $openedAfter = @fstat($handle);
    @fclose($handle);
    if (!is_array($openedBefore) || !is_array($openedAfter)
        || ($metadata['dev'] ?? null) !== ($openedBefore['dev'] ?? null)
        || ($metadata['ino'] ?? null) !== ($openedBefore['ino'] ?? null)
        || ($metadata['mode'] ?? null) !== ($openedBefore['mode'] ?? null)
        || ($metadata['nlink'] ?? null) !== ($openedBefore['nlink'] ?? null)
        || ($metadata['uid'] ?? null) !== ($openedBefore['uid'] ?? null)
        || ($metadata['gid'] ?? null) !== ($openedBefore['gid'] ?? null)
        || ($metadata['size'] ?? null) !== ($openedBefore['size'] ?? null)
        || ($metadata['mtime'] ?? null) !== ($openedBefore['mtime'] ?? null)
        || ($metadata['ctime'] ?? null) !== ($openedBefore['ctime'] ?? null)
        || (($openedBefore['mode'] ?? 0) & 0170000) !== 0100000
        || ($openedBefore['dev'] ?? null) !== ($openedAfter['dev'] ?? null)
        || ($openedBefore['ino'] ?? null) !== ($openedAfter['ino'] ?? null)
        || ($openedBefore['mode'] ?? null) !== ($openedAfter['mode'] ?? null)
        || ($openedBefore['uid'] ?? null) !== ($openedAfter['uid'] ?? null)
        || ($openedBefore['gid'] ?? null) !== ($openedAfter['gid'] ?? null)
        || ($openedBefore['size'] ?? null) !== ($openedAfter['size'] ?? null)
        || ($openedBefore['mtime'] ?? null) !== ($openedAfter['mtime'] ?? null)
        || ($openedBefore['ctime'] ?? null) !== ($openedAfter['ctime'] ?? null)
        || (int)($openedBefore['nlink'] ?? 0) !== 1) {
        $result['status'] = 'read_drift';
        $result['repairable'] = false;
        return $result;
    }

    $actualBlob = sha1('blob ' . strlen($actual) . "\0" . $actual);
    $result['actual_sha256'] = hash('sha256', $actual);
    if (!hash_equals($blob, $actualBlob)) {
        $normalized = str_replace("\r\n", "\n", $actual);
        $normalizedBlob = sha1('blob ' . strlen($normalized) . "\0" . $normalized);
        if (strpos($actual, "\r\n") !== false && hash_equals($blob, $normalizedBlob)) {
            $result['status'] = 'crlf_shebang';
            return $result;
        }
        $result['status'] = 'content_drift';
        $result['repairable'] = false;
        return $result;
    }
    if (($result['mode'] & 0777) !== 0755) {
        $result['status'] = 'invalid_mode';
        return $result;
    }
    if (substr($actual, 0, 12) !== "#!/bin/bash\n") {
        $result['status'] = 'invalid_shebang';
        $result['repairable'] = false;
        return $result;
    }
    if (substr($actual, 0, 13) === "#!/bin/bash\r\n") {
        $result['status'] = 'crlf_shebang';
        return $result;
    }

    $result['ok'] = true;
    $result['status'] = 'ok';
    $result['repairable'] = false;
    return $result;
}

function e3dcInspectServiceWrapper($wrapper) {
    $wrapper = (string)$wrapper;
    $result = [
        'ok' => false,
        'path' => $wrapper,
        'status' => 'missing',
    ];
    if ($wrapper === '' || is_link($wrapper)) {
        if ($wrapper !== '' && is_link($wrapper)) $result['status'] = 'symlink';
        return $result;
    }

    $paths = getInstallPaths();
    if (empty($paths['valid']) || empty($paths['install_path']) || empty($paths['install_user'])) {
        $result['status'] = 'install_context_unbound';
        return $result;
    }
    $repoDir = rtrim((string)$paths['install_path'], '/');
    $installerDir = $repoDir . '/Installer';
    $expected = '/usr/local/sbin/e3dc-service-control';
    if ($wrapper !== $expected
        || realpath($repoDir) !== $repoDir
        || realpath($installerDir) !== $installerDir
        || realpath($wrapper) !== $wrapper
        || is_link($repoDir)
        || is_link($installerDir)) {
        $result['status'] = 'unbound_path';
        return $result;
    }

    if (!function_exists('posix_getpwnam') || !function_exists('posix_getgrnam')) {
        $result['status'] = 'identity_unavailable';
        return $result;
    }
    foreach (['/usr/local', '/usr/local/sbin'] as $directory) {
        $directoryMeta = @lstat($directory);
        if (!is_array($directoryMeta)
            || (($directoryMeta['mode'] ?? 0) & 0170000) !== 0040000
            || (int)($directoryMeta['uid'] ?? -1) !== 0
            || (int)($directoryMeta['gid'] ?? -1) !== 0
            || (((int)($directoryMeta['mode'] ?? 0)) & 0022) !== 0) {
            $result['status'] = 'unsafe_path_permissions';
            return $result;
        }
    }

    clearstatcache(true, $wrapper);
    $metadata = @lstat($wrapper);
    if (!is_array($metadata)
        || (($metadata['mode'] ?? 0) & 0170000) !== 0100000
        || (int)($metadata['nlink'] ?? 0) !== 1) {
        $result['status'] = 'unsafe_file_type';
        return $result;
    }
    if ((int)($metadata['uid'] ?? -1) !== 0
        || (int)($metadata['gid'] ?? -1) !== 0
        || (((int)($metadata['mode'] ?? 0)) & 0777) !== 0755) {
        $result['status'] = 'unsafe_file_permissions';
        return $result;
    }

    $head = e3dcResolveGitObjectId($repoDir, 'HEAD^{commit}');
    $blob = $head ? e3dcResolveGitObjectId($repoDir, $head . ':Installer/service_wrapper.sh') : null;
    if ($head === null || $blob === null) {
        $result['status'] = 'head_unbound';
        return $result;
    }

    $handle = @fopen($wrapper, 'rb');
    if ($handle === false) {
        $result['status'] = 'not_readable';
        return $result;
    }
    $openedBefore = @fstat($handle);
    $actual = (string)@stream_get_contents($handle);
    $openedAfter = @fstat($handle);
    @fclose($handle);
    $pathAfter = @lstat($wrapper);
    foreach (['dev', 'ino', 'mode', 'nlink', 'uid', 'gid', 'size', 'mtime', 'ctime'] as $key) {
        if (!is_array($openedBefore)
            || !is_array($openedAfter)
            || !is_array($pathAfter)
            || ($metadata[$key] ?? null) !== ($openedBefore[$key] ?? null)
            || ($openedBefore[$key] ?? null) !== ($openedAfter[$key] ?? null)
            || ($openedAfter[$key] ?? null) !== ($pathAfter[$key] ?? null)) {
            $result['status'] = 'read_drift';
            return $result;
        }
    }

    $actualBlob = sha1('blob ' . strlen($actual) . "\0" . $actual);
    if (!hash_equals($blob, $actualBlob)
        || substr($actual, 0, 12) !== "#!/bin/bash\n"
        || strpos($actual, "\r") !== false) {
        $result['status'] = 'content_drift';
        return $result;
    }

    $result['ok'] = true;
    $result['status'] = 'ok';
    $result['head'] = $head;
    $result['actual_sha256'] = hash('sha256', $actual);
    $result['dev'] = (int)$metadata['dev'];
    $result['ino'] = (int)$metadata['ino'];
    return $result;
}

function e3dcInspectWebUpdateLauncher() {
    $launcher = '/usr/local/sbin/e3dc-web-update-launcher';
    $requiredContract = 'e3dc-download-bootstrap-v2';
    $result = ['ok' => false, 'path' => $launcher, 'status' => 'missing'];
    if (realpath($launcher) !== $launcher || is_link($launcher)) {
        $result['status'] = 'unbound_path';
        return $result;
    }
    foreach (['/', '/usr', '/usr/local', '/usr/local/sbin'] as $directory) {
        $metadata = @lstat($directory);
        if (!is_array($metadata)
            || (($metadata['mode'] ?? 0) & 0170000) !== 0040000
            || (int)($metadata['uid'] ?? -1) !== 0
            || (((int)($metadata['mode'] ?? 0)) & 0022) !== 0) {
            $result['status'] = 'unsafe_path_permissions';
            return $result;
        }
    }
    $metadata = @lstat($launcher);
    if (!is_array($metadata)
        || (($metadata['mode'] ?? 0) & 0170000) !== 0100000
        || (int)($metadata['nlink'] ?? 0) !== 1) {
        $result['status'] = 'unsafe_file_type';
        return $result;
    }
    $launcherMode = (int)($metadata['mode'] ?? 0);
    if ((int)($metadata['uid'] ?? -1) !== 0
        || ($launcherMode & 0022) !== 0
        || ($launcherMode & 0111) === 0) {
        $result['status'] = 'unsafe_file_permissions';
        return $result;
    }
    $handle = @fopen($launcher, 'rb');
    if ($handle === false) {
        clearstatcache(true, $launcher);
        $pathAfter = @lstat($launcher);
        foreach (['dev', 'ino', 'mode', 'nlink', 'uid', 'gid', 'size', 'mtime', 'ctime'] as $key) {
            if (!is_array($pathAfter)
                || ($metadata[$key] ?? null) !== ($pathAfter[$key] ?? null)) {
                $result['status'] = 'read_drift';
                return $result;
            }
        }
        $result['status'] = 'not_readable';
        return $result;
    }
    $openedBefore = @fstat($handle);
    $actual = @stream_get_contents($handle, 131073);
    $openedAfter = @fstat($handle);
    @fclose($handle);
    $pathAfter = @lstat($launcher);
    foreach (['dev', 'ino', 'mode', 'nlink', 'uid', 'gid', 'size', 'mtime', 'ctime'] as $key) {
        if (!is_array($openedBefore) || !is_array($openedAfter) || !is_array($pathAfter)
            || ($metadata[$key] ?? null) !== ($openedBefore[$key] ?? null)
            || ($openedBefore[$key] ?? null) !== ($openedAfter[$key] ?? null)
            || ($openedAfter[$key] ?? null) !== ($pathAfter[$key] ?? null)) {
            $result['status'] = 'read_drift';
            return $result;
        }
    }
    if (!is_string($actual)
        || substr($actual, 0, 12) !== "#!/bin/bash\n"
        || substr_count($actual, $requiredContract) !== 1) {
        $result['status'] = 'outdated_contract';
        return $result;
    }
    $result['ok'] = true;
    $result['status'] = 'ok';
    $result['inspection'] = 'stable_read';
    if (is_string($actual)) {
        $result['actual_sha256'] = hash('sha256', $actual);
    }
    $result['dev'] = (int)$metadata['dev'];
    $result['ino'] = (int)$metadata['ino'];
    return $result;
}

function e3dcInstallerWrapperIssueText($inspection) {
    $status = (string)($inspection['status'] ?? 'unknown');
    $messages = [
        'missing' => 'der Installer-Wrapper fehlt',
        'crlf_shebang' => 'der Installer-Wrapper hat Windows-Zeilenenden in der Shebang und kann deshalb vom Linux-Kernel nicht gestartet werden',
        'symlink' => 'der Installer-Wrapper ist ein Symlink und wird aus Sicherheitsgründen nicht automatisch verwendet',
        'hardlink' => 'der Installer-Wrapper hat mehrere Hardlinks und wird aus Sicherheitsgründen nicht automatisch verwendet',
        'not_regular' => 'der Installer-Wrapper ist keine reguläre Datei',
        'not_readable' => 'der Installer-Wrapper ist für den Webserver nicht lesbar',
        'invalid_shebang' => 'der Installer-Wrapper hat eine ungültige Shebang oder unbekannte Zeilenenden',
        'invalid_mode' => 'der Installer-Wrapper ist nicht mit dem freigegebenen Modus 0755 ausführbar',
        'content_drift' => 'der Installer-Wrapper weicht inhaltlich vom gebundenen Git-HEAD ab',
        'head_unbound' => 'der Installer-Wrapper konnte nicht an den lokalen Git-HEAD gebunden werden',
        'unbound_path' => 'der Installer-Wrapper liegt nicht am gebundenen Release-Pfad',
        'read_drift' => 'der Installer-Wrapper änderte sich während der Prüfung',
    ];
    return $messages[$status] ?? 'der Installer-Wrapper konnte nicht sicher geprüft werden';
}

function e3dcInstallerPrivilegeFailureMessage($operation, $baseDir, $wrapperInspection, $responses = []) {
    $operation = trim((string)$operation);
    $baseDir = rtrim((string)$baseDir, '/');
    $fixCmd = 'cd ' . escapeshellarg($baseDir) . ' && sudo python3 installer_main.py --fix-permissions';
    $responseText = implode("\n\n", array_filter((array)$responses));
    $cannotStart = ($operation === 'Web-Update')
        ? 'Web-Update kann nicht starten'
        : $operation . ' kann nicht starten';

    if (empty($wrapperInspection['ok'])) {
        $reason = e3dcInstallerWrapperIssueText($wrapperInspection);
        if (!empty($wrapperInspection['repairable'])) {
            $message = $cannotStart . ", weil " . $reason . ".\n"
                     . "Bitte einmal per SSH reparieren:\n" . $fixCmd;
        } else {
            $message = $cannotStart . ", weil " . $reason . ".\n"
                     . "Die automatische Ersetzung bleibt fail-closed gesperrt. Bitte den Wrapper gegen den freigegebenen Release-Stand prüfen.";
        }
    } else {
        $message = $cannotStart . ", weil die passwortlose sudo-Freigabe für den geprüften Installer-Wrapper fehlt.\n"
                 . "Bitte einmal per SSH ausführen:\n" . $fixCmd;
    }
    if ($responseText !== '') {
        $message .= "\n\nAntwort:\n" . $responseText;
    }
    return $message;
}

function handleReleaseRollback() {
    if (isset($_GET['action']) && $_GET['action'] === 'release_rollback_options') {
        requireWebAuth(true);
        header('Cache-Control: no-cache, no-store, must-revalidate');
        header('Content-Type: application/json');
        echo json_encode(e3dcBuildReleaseRollbackOptions());
        exit;
    }

    if (isset($_GET['action']) && $_GET['action'] === 'run_release_rollback') {
        header('Cache-Control: no-cache, no-store, must-revalidate');
        header('Content-Type: application/json');

        $mode = (string)($_GET['mode'] ?? $_POST['mode'] ?? '');
        $logFile = '/var/www/html/logs/release_rollback.log';
        $pidFile = '/var/www/html/tmp/release_rollback.pid';

        if ($mode === 'poll') {
            requireWebAuth(true);
            $log = file_exists($logFile) ? (string)@file_get_contents($logFile) : 'Status: Warte auf Start...';
            $running = false;
            if (file_exists($pidFile)) {
                $pid = (int)trim((string)@file_get_contents($pidFile));
                if ($pid > 0 && file_exists("/proc/$pid")) $running = true; else @unlink($pidFile);
            }
            $flags = 0;
            if (defined('JSON_INVALID_UTF8_SUBSTITUTE')) $flags |= JSON_INVALID_UTF8_SUBSTITUTE;
            if (defined('JSON_PARTIAL_OUTPUT_ON_ERROR')) $flags |= JSON_PARTIAL_OUTPUT_ON_ERROR;
            echo json_encode(['running' => $running, 'log' => $log], $flags);
            exit;
        }
        if ($mode !== 'start') {
            http_response_code(400);
            echo json_encode(['status' => 'error', 'message' => 'Ungültiger Rückfallmodus.']);
            exit;
        }
        e3dcRequirePostMutation(true);
        if (!e3dcPrivilegedInstallerWebActionsEnabled()) {
            echo json_encode([
                'status' => 'error',
                'message' => e3dcPrivilegedInstallerWebBlockMessage('Release-Rückfall'),
            ]);
            exit;
        }

        $tag = e3dcNormalizeReleaseTag($_POST['tag'] ?? '');
        $confirm = isset($_POST['confirm']) && $_POST['confirm'] === '1';
        $options = e3dcBuildReleaseRollbackOptions();
        $allowed = false;
        foreach ($options['releases'] as $release) {
            if (($release['tag'] ?? '') === $tag) {
                $allowed = true;
                break;
            }
        }
        if (!$tag || !$allowed) {
            echo json_encode(['status' => 'error', 'message' => 'Release-Tag ist nicht in der validierten Rückfallliste.']);
            exit;
        }
        if (!$confirm) {
            echo json_encode(['status' => 'error', 'message' => 'Rückfall erfordert Nutzerbestaetigung.']);
            exit;
        }
        if (e3dcIsDockerEnvironment()) {
            echo json_encode([
                'status' => 'docker',
                'message' => 'Docker-Rückfall wird nicht im Container ausgefuehrt.',
                'commands' => e3dcDockerReleaseCommandText($tag),
            ]);
            exit;
        }

        if (file_exists($pidFile)) {
            $pid = (int)trim((string)@file_get_contents($pidFile));
            if ($pid > 0 && file_exists("/proc/$pid")) {
                echo json_encode(['status' => 'running', 'message' => 'Rückfall läuft bereits.']);
                exit;
            }
            @unlink($pidFile);
        }

        $install = e3dcFindInstallerMainAndWrapper();
        if (!$install || empty($install['installer_main'])) {
            echo json_encode(['status' => 'error', 'message' => 'Installer nicht gefunden.']);
            exit;
        }

        @file_put_contents($logFile, "=== RELEASE-RUECKFALL START: $tag ===\n");
        @chmod($logFile, 0666);
        $wrapper = $install['wrapper'];
        $repoDir = $install['repo_dir'];
        $wrapperInspection = e3dcInspectInstallerWrapper($wrapper);
        $attempts = [];
        if ($wrapper && !empty($wrapperInspection['ok'])) {
            $attempts[] = [
                'label' => 'installer_wrapper.sh',
                'preflight' => 'sudo -n ' . escapeshellarg($wrapper) . ' check',
                'run' => 'sudo -n ' . escapeshellarg($wrapper) . ' install_release ' . escapeshellarg($tag),
            ];
        }
        $attempts[] = [
            'label' => 'installer_main.py direkt',
            'preflight' => 'sudo -n /usr/bin/python3 -I -B -u ' . escapeshellarg($install['installer_main']) . ' --check',
            'run' => 'sudo -n /usr/bin/python3 -I -B -u ' . escapeshellarg($install['installer_main']) . ' --install-release-tag ' . escapeshellarg($tag),
        ];

        $cmd = '';
        $sudoErrors = [];
        foreach ($attempts as $attempt) {
            $out = [];
            exec($attempt['preflight'] . ' 2>&1', $out, $ret);
            if ($ret === 0) {
                $cmd = $attempt['run'];
                $sudoErrors[] = $attempt['label'] . ': OK';
                break;
            }
            $sudoErrors[] = $attempt['label'] . " fehlgeschlagen:\n" . implode("\n", $out);
        }
        if ($cmd === '') {
            $msg = e3dcInstallerPrivilegeFailureMessage('Release-Rückfall', $repoDir, $wrapperInspection, $sudoErrors);
            @file_put_contents($logFile, $msg . "\n", FILE_APPEND);
            echo json_encode(['status' => 'error', 'message' => $msg]);
            exit;
        }

        @file_put_contents($logFile, "Start-Befehl: $cmd\n--------------------------------\n", FILE_APPEND);
        $pid = exec(sprintf('nohup %s >> %s 2>&1 & echo $!', $cmd, escapeshellarg($logFile)));
        if ($pid) {
            @file_put_contents($pidFile, $pid);
            @chmod($pidFile, 0666);
            echo json_encode(['status' => 'started', 'pid' => $pid]);
        } else {
            echo json_encode(['status' => 'error', 'message' => 'Konnte Release-Rückfall nicht starten.']);
        }
        exit;
    }
}

/**
 * Merkt sich die Update-Optionen aus der Web-UI.
 * Die eigentliche Aktualisierung läuft später über installer_main.py.
 */
function handleUpdatePreparation() {
    if (isset($_GET['action']) && $_GET['action'] === 'prepare_update') {
        e3dcRequirePostMutation(true);
        header('Content-Type: application/json');
        if (e3dcIsDockerEnvironment()) {
            echo json_encode([
                'success' => false,
                'docker' => true,
                'message' => e3dcDockerHostUpdateMessage(),
                'commands' => e3dcDockerHostUpdateCommandText(),
            ]);
            exit;
        }
        $force = isset($_POST['force']) && $_POST['force'] === 'true';
        $discard = isset($_POST['discard']) && $_POST['discard'] === 'true';
        $flagFile = '/var/www/html/ramdisk/e3dc_update_flags.json';
        file_put_contents($flagFile, json_encode(['force' => $force, 'discard' => $discard]));
        @chmod($flagFile, 0666);
        echo json_encode(['success' => true]);
        exit;
    }
}

/**
 * Vergleicht die installierte VERSION mit dem veröffentlichten Stable-Release.
 * Die Anzeige ist rein informativ und keine Voraussetzung für den Update-Start.
 */
function handleUpdateCheck() {
    if (isset($_GET['action']) && $_GET['action'] === 'check_update') {
        e3dcRequirePostMutation(true);
        header('Cache-Control: no-cache, no-store, must-revalidate');
        header('Pragma: no-cache');
        header('Expires: 0');
        header('Content-Type: application/json');

        if (e3dcIsDockerEnvironment()) {
            echo json_encode([
                'success' => true,
                'missing' => 0,
                'skipped' => true,
                'docker' => true,
                'message' => e3dcDockerHostUpdateMessage(),
                'commands' => e3dcDockerHostUpdateCommandText(),
            ]);
            exit;
        }

        $confData = loadE3dcConfig();
        $checkUpdates = $confData['config']['check_updates'] ?? '1';
        if ($checkUpdates === '0' && !isset($_GET['force_check'])) {
            echo json_encode(['success' => true, 'missing' => 0, 'skipped' => true]);
            exit;
        }

        $cacheFile = '/var/www/html/ramdisk/e3dc_update_status.json';
        if (!isset($_GET['force_check']) && file_exists($cacheFile) && (time() - filemtime($cacheFile) < 840)) {
            $cachedRaw = (string)file_get_contents($cacheFile);
            $cachedData = json_decode($cachedRaw, true);
            if (!is_array($cachedData) || empty($cachedData['updating'])) {
                echo $cachedRaw;
                exit;
            }
        }
        $result = e3dcStableUpdateCheck();
        file_put_contents($cacheFile, json_encode($result));
        @chmod($cacheFile, 0666);
        echo json_encode($result);
        exit;
    }
}

/**
 * Prüft den veröffentlichten Stable-Release für die Update-Anzeige.
 * Cache-Dauer: 4 Stunden; der eigentliche Update-Start bleibt davon unabhängig.
 */
function handleSelfUpdateCheck() {
    if (isset($_GET['action']) && $_GET['action'] === 'check_self_update') {
        e3dcRequirePostMutation(true);
        header('Cache-Control: no-cache, no-store, must-revalidate');
        header('Content-Type: application/json');

        if (e3dcIsDockerEnvironment()) {
            echo json_encode([
                'success' => true,
                'missing' => 0,
                'skipped' => true,
                'docker' => true,
                'message' => e3dcDockerHostUpdateMessage(),
                'commands' => e3dcDockerHostUpdateCommandText(),
            ]);
            exit;
        }

        $cacheFile = '/var/www/html/ramdisk/e3dc_self_update_status.json';
        $forceCheck = isset($_GET['force']) || isset($_GET['force_check']);
        if (!$forceCheck && file_exists($cacheFile) && (time() - filemtime($cacheFile) < 14400)) {
            $cachedRaw = (string)file_get_contents($cacheFile);
            $cachedData = json_decode($cachedRaw, true);
            if (!is_array($cachedData) || empty($cachedData['updating'])) {
                echo $cachedRaw;
                exit;
            }
            // Ein Startmarker ist kein vier Stunden gültiges Prüfergebnis.
            // Nach dem Start folgt deshalb wieder der normale Stable-Vergleich.
        }
        $result = e3dcStableUpdateCheck();
        file_put_contents($cacheFile, json_encode($result));
        @chmod($cacheFile, 0666);
        echo json_encode($result);
        exit;
    }
}

/**
 * Führt das Installer-Update (Diagramme) aus.
 */
function e3dcSelfUpdateLogHasCanonicalSuccess($log) {
    return preg_match(
        '/(?:^|\R)\[OK\]\s+(?:self-update auf [0-9a-f]{40} abgeschlossen\.|'
        . 'Du bist auf dem neuesten Stand: [0-9a-f]{40}\.|'
        . 'Update abgeschlossen\.\s*\RVersion:\s*v?\d+\.\d+\.\d+[A-Za-z0-9._-]*)'
        . '\s*(?:\R|$)/i',
        (string)$log
    ) === 1;
}

function e3dcSelfUpdateLogHasTerminalFailure($log) {
    return preg_match(
        '/traceback|exception|critical|fatal|permission denied|'
        . '\[!\]\s+self-update fehlgeschlagen|self-update fehlgeschlagen:|'
        . '\[!\]\s+web-update fehlgeschlagen|'
        . 'REPAIR_REQUIRED|'
        . 'web-update kann nicht starten|konnte update-prozess nicht starten/i',
        (string)$log
    ) === 1;
}

function e3dcClassifySelfUpdateCompletion($running, $exitCode, $log) {
    if ($running === true) {
        return 'running';
    }
    if (is_int($exitCode)) {
        return ($exitCode === 0) ? 'success' : 'failed';
    }
    if (e3dcSelfUpdateLogHasTerminalFailure($log)) {
        return 'failed';
    }
    if (e3dcSelfUpdateLogHasCanonicalSuccess($log)) {
        return 'success';
    }
    return 'unknown';
}

function handleRunSelfUpdate() {
    if (isset($_GET['action']) && $_GET['action'] === 'poll_self_update') {
        requireWebAuth(true);
        header('Cache-Control: no-cache, no-store, must-revalidate');
        header('Content-Type: application/json');

        $logFile = '/var/log/e3dc-control/web-update.log';
        $pidFile = '/run/e3dc-web-update/pid';
        $statusFile = '/run/e3dc-web-update/status';
        $log = 'Status: Initialisiere... (Log-Datei noch nicht erstellt)';
        if (file_exists($logFile)) {
            $content = file_get_contents($logFile);
            $log = ($content === false) ? 'Log-Datei existiert, kann aber nicht gelesen werden.' : $content;
        }

        $running = false;
        if (file_exists($pidFile)) {
            $pid = (int)trim(file_get_contents($pidFile));
            if ($pid > 0 && file_exists("/proc/$pid")) {
                $running = true;
            }
        }

        $exitCode = null;
        if (file_exists($statusFile)) {
            $rawExitCode = trim((string)file_get_contents($statusFile));
            if (preg_match('/^-?\d+$/', $rawExitCode)) {
                $exitCode = (int)$rawExitCode;
            }
        }

        $completion = e3dcClassifySelfUpdateCompletion($running, $exitCode, $log);
        if ($completion === 'success' && $exitCode === null) {
            // Kompatibilitätsbrücke für einen Lauf, den eine ältere Webdatei
            // noch ohne Exitcode-Datei gestartet hat. Der zurückgegebene
            // Marker wird auch von älteren bereits geladenen Browsern erkannt.
            if (strpos($log, '[OK] Update abgeschlossen.') === false) {
                $log .= "\n[OK] Update abgeschlossen.\n";
                $installedVersion = readInstalledVersion();
                if ($installedVersion !== '') {
                    $log .= 'Version: ' . $installedVersion . "\n";
                }
            }
        }

        $flags = 0;
        if (defined('JSON_INVALID_UTF8_SUBSTITUTE')) $flags |= JSON_INVALID_UTF8_SUBSTITUTE;
        if (defined('JSON_PARTIAL_OUTPUT_ON_ERROR')) $flags |= JSON_PARTIAL_OUTPUT_ON_ERROR;
        echo json_encode([
            'success' => true,
            'running' => $running,
            'log' => $log,
            'exit_code' => $exitCode,
            'completion' => $completion,
        ], $flags);
        exit;
    }

    if (isset($_GET['action']) && $_GET['action'] === 'run_self_update') {
        e3dcRequirePostMutation(true);
        header('Content-Type: application/json');

        if (e3dcIsDockerEnvironment()) {
            echo json_encode([
                'success' => false,
                'docker' => true,
                'message' => e3dcDockerHostUpdateMessage(),
                'commands' => e3dcDockerHostUpdateCommandText(),
            ]);
            exit;
        }
        $reinstallRaw = isset($_POST['reinstall']) ? (string)$_POST['reinstall'] : '0';
        if ($reinstallRaw !== '0') {
            echo json_encode([
                'success' => false,
                'message' => 'Über das Web ist ausschließlich das reguläre Update ohne Neuinstallation zulässig.',
            ]);
            exit;
        }
        $launcherInspection = e3dcInspectWebUpdateLauncher();
        if (empty($launcherInspection['ok'])) {
            echo json_encode([
                'success' => false,
                'message' => 'Der root-eigene Web-Update-Launcher ist nicht sicher gebunden ('
                    . (string)($launcherInspection['status'] ?? 'unbekannt')
                    . "). Lösung: Führe einmalig diesen Befehl aus:\n"
                    . 'bootstrap_file="$(mktemp)" && '
                    . "curl -q -fsS --proto '=https' --tlsv1.2 "
                    . '-o "$bootstrap_file" '
                    . "https://raw.githubusercontent.com/A9xxx/Install-E3DC-Control/v5.4.4c/e3dc-update-bootstrap "
                    . '&& sudo /bin/sh "$bootstrap_file"; rc=$?; '
                    . 'rm -f -- "$bootstrap_file"; exit $rc',
            ]);
            exit;
        }
        $zeroPayload = json_encode(['success' => true, 'missing' => 0, 'updating' => true]);
        foreach (['/var/www/html/ramdisk/e3dc_self_update_status.json', '/var/www/html/ramdisk/e3dc_update_status.json'] as $cacheFile) {
            @file_put_contents($cacheFile, $zeroPayload);
            @chmod($cacheFile, 0666);
        }
        $output = [];
        $exitCode = 1;
        exec('/usr/bin/sudo -n -- /usr/local/sbin/e3dc-web-update-launcher 2>&1', $output, $exitCode);
        if ($exitCode === 0) {
            echo json_encode([
                'success' => true,
                'running' => true,
                'message' => trim(implode("\n", $output)) ?: 'Update als root-kontrollierten Systemjob gestartet.',
            ]);
        } else {
            echo json_encode([
                'success' => false,
                'message' => 'Web-Update konnte den engen Launcher nicht starten: ' . trim(implode("\n", $output)),
            ]);
        }
        exit;
    }
}

/**
 * Führt den Neustart des E3DC-Services aus.
 */
function e3dcSystemdServiceProperty($property, $service) {
    $property = trim((string)$property);
    $service = trim((string)$service);
    if (!in_array($property, ['is-active', 'is-enabled'], true)
        || $service === ''
        || !preg_match('/^[A-Za-z0-9_.@-]+$/', $service)) {
        return '';
    }
    if (!str_ends_with($service, '.service')) {
        $service .= '.service';
    }
    $systemctl = '/usr/bin/systemctl';
    if (!is_file($systemctl) || !is_executable($systemctl)) {
        return '';
    }
    $process = e3dcRunArgvProcess(
        [$systemctl, $property, '--', $service],
        10.0,
        ['max_output_bytes' => 8192]
    );
    return trim((string)($process['stdout'] ?? ''));
}

function e3dcSystemdServiceStatus($service) {
    $status = e3dcSystemdServiceProperty('is-active', $service);
    return $status !== '' ? $status : 'unknown';
}

function e3dcFindServiceWrapper() {
    $wrapperCandidates = ['/usr/local/sbin/e3dc-service-control'];
    foreach ($wrapperCandidates as $candidate) {
        $inspection = e3dcInspectServiceWrapper($candidate);
        if (!empty($inspection['ok'])) {
            return $candidate;
        }
    }
    return null;
}

function e3dcRunServiceWrapperAction($action, array $services) {
    $action = trim((string)$action);
    if (!in_array($action, ['start', 'stop', 'restart', 'status', 'enable', 'disable'], true)) {
        return [
            'success' => false,
            'changed' => [],
            'ignored' => [],
            'errors' => ['Unzulaessige Dienstaktion: ' . $action],
        ];
    }

    $serviceWrapper = e3dcFindServiceWrapper();
    if (!$serviceWrapper) {
        return [
            'success' => false,
            'changed' => [],
            'ignored' => [],
            'errors' => ['Service-Wrapper nicht gefunden. Bitte Rechte-Reparatur ausfuehren.'],
        ];
    }

    $sudo = '/usr/bin/sudo';
    if (!is_file($sudo) || !is_executable($sudo)) {
        return [
            'success' => false,
            'changed' => [],
            'ignored' => [],
            'output' => '',
            'errors' => ['sudo ist für den Service-Wrapper nicht verfügbar.'],
        ];
    }

    $changed = [];
    $ignored = [];
    $errors = [];
    $output = [];
    foreach ($services as $service) {
        $service = trim((string)$service);
        if ($service === '' || !preg_match('/^[A-Za-z0-9_.@-]+$/', $service)) {
            $errors[] = 'Unzulässiger Dienstname: ' . $service;
            continue;
        }
        if (!str_ends_with($service, '.service')) {
            $service .= '.service';
        }
        $process = e3dcRunArgvProcess(
            [$sudo, '-n', $serviceWrapper, $action, $service],
            30.0,
            ['max_output_bytes' => 32768]
        );
        $code = (int)($process['exit_code'] ?? 1);
        $text = trim((string)($process['stdout'] ?? '') . "\n" . (string)($process['stderr'] ?? ''));
        if ($text !== '') {
            $output[] = $service . ': ' . $text;
        }
        $isMissing = preg_match('/not found|not loaded|could not be found|does not exist|nicht gefunden|ist nicht geladen|Unit .* not found/i', $text);
        if (!empty($process['success']) && $code === 0) {
            $changed[] = $service;
        } elseif ($isMissing) {
            $ignored[] = $service;
        } else {
            $detail = $text !== '' ? $text : (string)($process['error'] ?? 'unbekannter Fehler');
            if (!empty($process['timed_out'])) $detail = 'Timeout: ' . $detail;
            if ((int)($process['signal'] ?? 0) > 0) $detail = 'Signal ' . (int)$process['signal'] . ': ' . $detail;
            $errors[] = $service . ' (rc=' . $code . '): ' . $detail;
        }
    }

    return [
        'success' => empty($errors),
        'changed' => $changed,
        'ignored' => $ignored,
        'output' => implode("\n", $output),
        'errors' => $errors,
    ];
}

/**
 * Prüft ausschließlich, ob die fest gebundene Prognosediagnose-Unit vorhanden ist.
 */
function e3dcForecastEvidenceUnitExists() {
    $unit = 'e3dc-forecast-evidence.service';
    foreach (['/etc/systemd/system', '/lib/systemd/system', '/usr/lib/systemd/system'] as $base) {
        if (is_file($base . '/' . $unit)) {
            return true;
        }
    }
    return false;
}

/**
 * Liefert den beweisbaren persistenten und aktuellen Zustand der Prognosediagnose.
 *
 * Andere systemd-Zustände als enabled/disabled und active/inactive werden nicht
 * geschätzt. Insbesondere masked, failed, activating oder leere Antworten führen
 * zu einem geschlossenen Vertrag.
 */
function e3dcForecastEvidenceServiceState() {
    $unit = 'e3dc-forecast-evidence.service';
    $exists = e3dcForecastEvidenceUnitExists();
    $activeRaw = e3dcSystemdServiceProperty('is-active', $unit);
    $enabledRaw = e3dcSystemdServiceProperty('is-enabled', $unit);
    $activeValid = in_array($activeRaw, ['active', 'inactive'], true);
    $enabledValid = in_array($enabledRaw, ['enabled', 'disabled'], true);
    return [
        'unit' => $unit,
        'exists' => $exists,
        'valid' => $exists && $activeValid && $enabledValid,
        'active' => $activeRaw === 'active',
        'enabled' => $enabledRaw === 'enabled',
        'active_raw' => $activeRaw !== '' ? $activeRaw : 'unknown',
        'enabled_raw' => $enabledRaw !== '' ? $enabledRaw : 'unknown',
    ];
}

function e3dcForecastEvidenceStateIsProven($state) {
    return is_array($state)
        && ($state['exists'] ?? null) === true
        && ($state['valid'] ?? null) === true
        && array_key_exists('active', $state)
        && is_bool($state['active'])
        && array_key_exists('enabled', $state)
        && is_bool($state['enabled']);
}

/**
 * Führt genau eine der vier für den festen Dienst nötigen Zustandsänderungen aus
 * und beweist den erwarteten Nachzustand. Die Callbacks dienen nur der testbaren
 * Transaktionslogik; das öffentliche Eintrittstor bindet immer dieselbe Unit.
 */
function e3dcForecastEvidenceApplyVerifiedAction(
    $action,
    callable $readState,
    callable $applyAction,
    $stabilityCheck = null
) {
    $expectations = [
        'enable' => ['enabled', true],
        'disable' => ['enabled', false],
        'start' => ['active', true],
        'stop' => ['active', false],
    ];
    if (!isset($expectations[$action])) {
        return [
            'ok' => false,
            'action' => (string)$action,
            'wrapper_success' => false,
            'state_proven' => false,
            'verified' => false,
            'state_after' => [],
            'errors' => ['Unzulässige Prognosediagnose-Dienstaktion.'],
        ];
    }

    try {
        $wrapper = $applyAction($action);
    } catch (Throwable $exc) {
        $wrapper = [
            'success' => false,
            'errors' => ['Ausnahme bei Dienstaktion: ' . $exc->getMessage()],
        ];
    }
    if (!is_array($wrapper)) {
        $wrapper = ['success' => false, 'errors' => ['Ungültige Wrapper-Antwort.']];
    }

    try {
        $after = $readState();
    } catch (Throwable $exc) {
        $after = [
            'exists' => false,
            'valid' => false,
            'active' => false,
            'enabled' => false,
            'active_raw' => 'unknown',
            'enabled_raw' => 'unknown',
        ];
    }
    $stateProven = e3dcForecastEvidenceStateIsProven($after);
    [$expectedKey, $expectedValue] = $expectations[$action];
    $verified = $stateProven && ($after[$expectedKey] === $expectedValue);
    $wrapperReportedSuccess = ($wrapper['success'] ?? null) === true;
    $changed = array_values(array_map('strval', (array)($wrapper['changed'] ?? [])));
    $changedProven = in_array('e3dc-forecast-evidence.service', $changed, true);
    $stability = null;
    if ($action === 'start'
        && $wrapperReportedSuccess
        && $changedProven
        && $verified
        && is_callable($stabilityCheck)
    ) {
        try {
            $stability = $stabilityCheck();
        } catch (Throwable $exc) {
            $stability = [
                'success' => false,
                'state' => [],
                'observations' => [],
                'error' => $exc->getMessage(),
            ];
        }
        if (!is_array($stability)) {
            $stability = ['success' => false, 'state' => [], 'observations' => []];
        }
        $stableState = is_array($stability['state'] ?? null) ? $stability['state'] : [];
        $after = $stableState;
        $stateProven = e3dcForecastEvidenceStateIsProven($after);
        $verified = ($stability['success'] ?? null) === true
            && $stateProven
            && $after['active'] === true;
    }
    $wrapperSuccess = $wrapperReportedSuccess && $changedProven;
    return [
        'ok' => $wrapperSuccess && $verified,
        'action' => $action,
        'wrapper_success' => $wrapperSuccess,
        'wrapper_reported_success' => $wrapperReportedSuccess,
        'changed_proven' => $changedProven,
        'state_proven' => $stateProven,
        'verified' => $verified,
        'state_after' => is_array($after) ? $after : [],
        'stability' => $stability,
        'output' => (string)($wrapper['output'] ?? ''),
        'errors' => array_values(array_map('strval', (array)($wrapper['errors'] ?? []))),
    ];
}

/**
 * Beweist nach einem Start über mehrere Beobachtungen, dass die Unit nicht nur
 * kurz aktiv wird und anschließend in den Restart-Backoff fällt.
 */
function e3dcForecastEvidenceConfirmStableActiveState() {
    $observations = [];
    $last = [];
    for ($index = 0; $index < 4; $index++) {
        if ($index > 0) {
            usleep(1000000);
        }
        $last = e3dcForecastEvidenceServiceState();
        $observations[] = $last;
        if (!e3dcForecastEvidenceStateIsProven($last) || $last['active'] !== true) {
            return [
                'success' => false,
                'state' => $last,
                'observations' => $observations,
            ];
        }
    }
    return [
        'success' => true,
        'state' => $last,
        'observations' => $observations,
    ];
}

/**
 * Aktiviert und startet die Prognosediagnose transaktional.
 *
 * Scheitert ein Schritt oder seine Nachprüfung, wird der exakt bewiesene
 * enabled/active-Vorzustand wiederhergestellt. Kann der Zustand nicht bewiesen
 * werden, bleibt die Funktion fail-closed.
 */
function e3dcForecastEvidenceActivationTransaction(
    callable $readState,
    callable $applyAction,
    $stabilityCheck = null
) {
    $safeRead = function () use ($readState) {
        try {
            $state = $readState();
            return is_array($state) ? $state : [];
        } catch (Throwable $exc) {
            return [];
        }
    };

    $before = $safeRead();
    $result = [
        'success' => false,
        'noop' => false,
        'message' => '',
        'error_code' => '',
        'state_before' => $before,
        'steps' => [],
        'state_after' => $before,
        'rollback' => [
            'attempted' => false,
            'success' => false,
            'steps' => [],
        ],
    ];
    if (!e3dcForecastEvidenceStateIsProven($before)) {
        $result['error_code'] = 'forecast_evidence_state_unproven';
        $result['message'] = 'Der Zustand der PV-Prognosediagnose ist nicht eindeutig beweisbar; es wurde nichts geändert.';
        return $result;
    }

    if ($before['enabled'] === true && $before['active'] === true) {
        $result['success'] = true;
        $result['noop'] = true;
        $result['message'] = 'Die PV-Prognosediagnose ist bereits dauerhaft aktiviert und läuft.';
        $result['rollback']['success'] = true;
        return $result;
    }

    $current = $before;
    $failedStep = null;
    if ($current['enabled'] !== true) {
        $step = e3dcForecastEvidenceApplyVerifiedAction(
            'enable',
            $readState,
            $applyAction,
            $stabilityCheck
        );
        $result['steps'][] = $step;
        $current = $step['state_after'];
        if (empty($step['ok'])) {
            $failedStep = $step;
        }
    }
    if ($failedStep === null && ($current['active'] ?? null) !== true) {
        $step = e3dcForecastEvidenceApplyVerifiedAction(
            'start',
            $readState,
            $applyAction,
            $stabilityCheck
        );
        $result['steps'][] = $step;
        $current = $step['state_after'];
        if (empty($step['ok'])) {
            $failedStep = $step;
        }
    }

    if ($failedStep === null
        && e3dcForecastEvidenceStateIsProven($current)
        && $current['enabled'] === true
        && $current['active'] === true
    ) {
        $result['success'] = true;
        $result['message'] = 'Die PV-Prognosediagnose wurde dauerhaft aktiviert und gestartet.';
        $result['state_after'] = $current;
        $result['rollback']['success'] = true;
        return $result;
    }

    if ($failedStep === null) {
        $failedStep = [
            'action' => 'verify',
            'wrapper_success' => true,
            'state_proven' => e3dcForecastEvidenceStateIsProven($current),
            'verified' => false,
            'state_after' => $current,
            'errors' => ['Der Zielzustand enabled und active wurde nicht erreicht.'],
        ];
    }
    $result['error_code'] = !empty($failedStep['wrapper_success'])
        ? 'forecast_evidence_activation_verification_failed'
        : 'forecast_evidence_activation_step_failed';
    $result['failure'] = $failedStep;
    $result['rollback']['attempted'] = true;

    $rollbackState = $safeRead();
    $rollbackActiveRaw = (string)($rollbackState['active_raw'] ?? 'unknown');
    $activeStatesRequiringStop = ['active', 'failed', 'activating', 'deactivating', 'reloading'];
    $needsStop = false;
    if (($before['active_raw'] ?? '') === 'inactive') {
        $needsStop = in_array($rollbackActiveRaw, $activeStatesRequiringStop, true);
    } elseif (($before['active_raw'] ?? '') === 'active') {
        $needsStop = in_array(
            $rollbackActiveRaw,
            ['failed', 'activating', 'deactivating', 'reloading'],
            true
        );
    }
    if ($needsStop) {
        $rollbackStep = e3dcForecastEvidenceApplyVerifiedAction(
            'stop',
            $readState,
            $applyAction,
            $stabilityCheck
        );
        $result['rollback']['steps'][] = $rollbackStep;
        $rollbackState = $rollbackStep['state_after'];
    }

    $rollbackEnabledRaw = (string)($rollbackState['enabled_raw'] ?? 'unknown');
    if (in_array($rollbackEnabledRaw, ['enabled', 'disabled'], true)
        && $rollbackEnabledRaw !== ($before['enabled_raw'] ?? '')
    ) {
        $rollbackAction = ($before['enabled_raw'] ?? '') === 'enabled'
            ? 'enable'
            : 'disable';
        $rollbackStep = e3dcForecastEvidenceApplyVerifiedAction(
            $rollbackAction,
            $readState,
            $applyAction,
            $stabilityCheck
        );
        $result['rollback']['steps'][] = $rollbackStep;
        $rollbackState = $rollbackStep['state_after'];
    }

    $rollbackActiveRaw = (string)($rollbackState['active_raw'] ?? 'unknown');
    if (($before['active_raw'] ?? '') === 'active' && $rollbackActiveRaw === 'inactive') {
        $rollbackStep = e3dcForecastEvidenceApplyVerifiedAction(
            'start',
            $readState,
            $applyAction,
            $stabilityCheck
        );
        $result['rollback']['steps'][] = $rollbackStep;
        $rollbackState = $rollbackStep['state_after'];
    }

    $finalState = $safeRead();
    $rollbackOk = e3dcForecastEvidenceStateIsProven($finalState)
        && ($finalState['enabled_raw'] ?? '') === ($before['enabled_raw'] ?? '')
        && ($finalState['active_raw'] ?? '') === ($before['active_raw'] ?? '');
    $result['rollback']['success'] = $rollbackOk;
    $result['state_after'] = $finalState;
    if ($rollbackOk) {
        $result['message'] = 'Die Aktivierung ist fehlgeschlagen; der vorherige Dienstzustand wurde wiederhergestellt.';
    } else {
        $result['error_code'] = 'forecast_evidence_rollback_failed';
        $result['message'] = 'Die Aktivierung und die Wiederherstellung des vorherigen Dienstzustands sind fehlgeschlagen. Bitte den Dienst administrativ prüfen.';
    }
    return $result;
}

/**
 * Enges öffentliches Eintrittstor: kein freier Dienstname, keine freie Aktion.
 */
function e3dcActivateForecastEvidenceService() {
    $unit = 'e3dc-forecast-evidence.service';
    $baseFailure = function ($code, $message) use ($unit) {
        return [
            'success' => false,
            'noop' => false,
            'service' => $unit,
            'error_code' => $code,
            'error' => $message,
            'message' => $message,
            'status' => 'unknown',
            'active' => false,
            'enabled' => false,
            'enabled_known' => false,
            'state_before' => [],
            'steps' => [],
            'state_after' => [],
            'rollback' => ['attempted' => false, 'success' => false, 'steps' => []],
        ];
    };

    if (e3dcIsDockerEnvironment()) {
        return $baseFailure(
            'forecast_evidence_docker_blocked',
            'Im Docker-Betrieb wird die Prognosediagnose ausschließlich über den Containerstart aktiviert.'
        );
    }
    if (!e3dcForecastEvidenceUnitExists()) {
        return $baseFailure(
            'unit_missing',
            'Die Service-Datei der PV-Prognosediagnose fehlt; es wurde nichts installiert oder geändert.'
        );
    }
    if (!e3dcFindServiceWrapper()) {
        return $baseFailure(
            'service_wrapper_unavailable',
            'Der geprüfte Service-Wrapper ist nicht verfügbar; es wurde nichts geändert.'
        );
    }

    $lockPath = '/var/www/html/ramdisk/.forecast_evidence_activation.lock';
    $lock = @fopen($lockPath, 'c');
    $lockStat = is_resource($lock) ? @fstat($lock) : false;
    $lockPathStat = @lstat($lockPath);
    $lockIsRegular = is_array($lockStat)
        && (($lockStat['mode'] & 0170000) === 0100000)
        && (int)($lockStat['nlink'] ?? 0) === 1;
    $lockPathMatches = is_array($lockStat)
        && is_array($lockPathStat)
        && (int)($lockStat['dev'] ?? -1) === (int)($lockPathStat['dev'] ?? -2)
        && (int)($lockStat['ino'] ?? -1) === (int)($lockPathStat['ino'] ?? -2);
    if ($lock === false || !$lockIsRegular || !$lockPathMatches) {
        if (is_resource($lock)) {
            @fclose($lock);
        }
        return $baseFailure(
            'forecast_evidence_activation_lock_invalid',
            'Die feste Sperrdatei der PV-Prognosediagnose ist nicht sicher verwendbar; es wurde nichts geändert.'
        );
    }
    if (!@flock($lock, LOCK_EX | LOCK_NB)) {
        @fclose($lock);
        return $baseFailure(
            'forecast_evidence_activation_locked',
            'Eine andere Aktivierung der PV-Prognosediagnose läuft bereits.'
        );
    }

    try {
        $result = e3dcForecastEvidenceActivationTransaction(
            function () {
                return e3dcForecastEvidenceServiceState();
            },
            function ($action) use ($unit) {
                return e3dcRunServiceWrapperAction($action, [$unit]);
            },
            function () {
                return e3dcForecastEvidenceConfirmStableActiveState();
            }
        );
    } finally {
        @flock($lock, LOCK_UN);
        @fclose($lock);
    }

    $result['service'] = $unit;
    $after = is_array($result['state_after'] ?? null) ? $result['state_after'] : [];
    $result['status'] = (string)($after['active_raw'] ?? 'unknown');
    $result['active'] = ($after['active'] ?? null) === true;
    $result['enabled'] = ($after['enabled'] ?? null) === true;
    $result['enabled_known'] = in_array(
        (string)($after['enabled_raw'] ?? ''),
        ['enabled', 'disabled'],
        true
    );
    if (empty($result['success'])) {
        $result['error'] = (string)($result['message'] ?? 'Aktivierung fehlgeschlagen.');
    }
    $output = [];
    foreach (array_merge(
        (array)($result['steps'] ?? []),
        (array)($result['rollback']['steps'] ?? [])
    ) as $step) {
        $text = trim((string)($step['output'] ?? ''));
        if ($text !== '') {
            $output[] = (string)($step['action'] ?? 'Aktion') . ': ' . $text;
        }
    }
    $result['output'] = implode("\n", $output);
    return $result;
}

function handleServiceRestart() {
    if (isset($_GET['action']) && $_GET['action'] === 'restart_service') {
        requireWebAuth(true);
        header('Content-Type: application/json');
        if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
            http_response_code(405);
            echo json_encode(['success' => false, 'message' => 'Dienstneustart ist nur per POST erlaubt.']);
            exit;
        }
        e3dcRequireCsrfToken(true);

        $serviceWrapper = null;
        if (!file_exists('/.dockerenv')) {
            // Vor jeder Zustandsänderung an Release-Root, Owner, Mode, Inode
            // und Git-HEAD binden.
            $serviceWrapper = e3dcFindServiceWrapper();
            if (!$serviceWrapper) {
                echo json_encode([
                    'success' => false,
                    'message' => 'Service-Wrapper nicht gefunden. Bitte Rechte-Reparatur ausführen.'
                ]);
                exit;
            }
        }

        // RAM-Disk Variablen und Speicher-Flags vor Neustart löschen (Notfall-Reset)
        $flags = [
            '/var/www/html/ramdisk/manual_boost.flag',
            '/var/www/html/ramdisk/pv_boost.flag',
            '/var/www/html/ramdisk/wallbox_force.flag',
            '/var/www/html/data/morning_boost_state.json',
            '/var/www/html/data/home_soc_state.json'
        ];
        foreach($flags as $f) { if (file_exists($f)) @unlink($f); }

        if (file_exists('/.dockerenv')) {
            $restartFlag = '/var/www/html/ramdisk/restart_container.flag';
            $existingFlag = @lstat($restartFlag);
            if ($existingFlag !== false) {
                $existingType = ((int)$existingFlag['mode']) & 0170000;
                $existingSafe = (
                    $existingType !== 0100000
                    ? false
                    : (
                        (int)($existingFlag['nlink'] ?? 0) === 1
                        && (int)($existingFlag['size'] ?? -1) === 2
                        && ((((int)$existingFlag['mode']) & 0777) === 0660)
                    )
                );
                $existingPayload = false;
                $openedExisting = false;
                if ($existingSafe) {
                    $existingHandle = @fopen($restartFlag, 'rb');
                    if (is_resource($existingHandle)) {
                        $existingPayload = @fread($existingHandle, 3);
                        $openedExisting = @fstat($existingHandle);
                        @fclose($existingHandle);
                    }
                    @clearstatcache(true, $restartFlag);
                    $existingAfterRead = @lstat($restartFlag);
                    $existingSafe = (
                        $existingPayload === "1\n"
                        && is_array($openedExisting)
                        && is_array($existingAfterRead)
                        && (int)($openedExisting['dev'] ?? -1) === (int)($existingFlag['dev'] ?? -2)
                        && (int)($openedExisting['ino'] ?? -1) === (int)($existingFlag['ino'] ?? -2)
                        && (int)($existingAfterRead['dev'] ?? -1) === (int)($existingFlag['dev'] ?? -2)
                        && (int)($existingAfterRead['ino'] ?? -1) === (int)($existingFlag['ino'] ?? -2)
                        && (int)($existingAfterRead['nlink'] ?? 0) === 1
                        && (int)($existingAfterRead['size'] ?? -1) === 2
                    );
                }
                if (!$existingSafe) {
                    echo json_encode([
                        'success' => false,
                        'message' => 'Unsicheres Docker-Neustartflag erkannt; Neustart wurde nicht angefordert.'
                    ]);
                    exit;
                }
                echo json_encode([
                    'success' => true,
                    'message' => 'Docker-Neustart ist bereits sicher angefordert.'
                ]);
                exit;
            }

            $previousUmask = @umask(0007);
            $restartHandle = @fopen($restartFlag, 'x+b');
            if (is_int($previousUmask)) {
                @umask($previousUmask);
            }
            $restartOk = is_resource($restartHandle);
            $restartAlreadyQueued = false;
            $createdFlagIdentity = null;
            if ($restartOk) {
                $restartOk = (@fwrite($restartHandle, "1\n") === 2);
                $restartOk = (@fflush($restartHandle) && $restartOk);
                $openedFlag = @fstat($restartHandle);
                if (is_array($openedFlag)) {
                    $createdFlagIdentity = [
                        'dev' => (int)($openedFlag['dev'] ?? -1),
                        'ino' => (int)($openedFlag['ino'] ?? -1),
                    ];
                }
                $restartOk = (
                    $restartOk
                    && is_array($openedFlag)
                    && ((((int)$openedFlag['mode']) & 0170000) === 0100000)
                    && ((int)($openedFlag['nlink'] ?? 0) === 1)
                    && ((((int)$openedFlag['mode']) & 0777) === 0660)
                );
                @fclose($restartHandle);
            } else {
                // Zwei gleichzeitige POSTs dürfen ein bereits sicher erzeugtes
                // Flag nicht gegenseitig entfernen.
                $racedFlag = @lstat($restartFlag);
                $restartAlreadyQueued = (
                    is_array($racedFlag)
                    && ((((int)$racedFlag['mode']) & 0170000) === 0100000)
                    && ((int)($racedFlag['nlink'] ?? 0) === 1)
                    && ((int)($racedFlag['size'] ?? -1) === 2)
                    && ((((int)$racedFlag['mode']) & 0777) === 0660)
                );
            }
            @clearstatcache(true, $restartFlag);
            $finalFlag = @lstat($restartFlag);
            $finalPayload = false;
            $openedFinalFlag = false;
            $namedFinalFlag = false;
            if (is_array($finalFlag)) {
                $finalHandle = @fopen($restartFlag, 'rb');
                if (is_resource($finalHandle)) {
                    $finalPayload = @fread($finalHandle, 3);
                    $openedFinalFlag = @fstat($finalHandle);
                    @fclose($finalHandle);
                }
                @clearstatcache(true, $restartFlag);
                $namedFinalFlag = @lstat($restartFlag);
            }
            $finalContentStable = (
                $finalPayload === "1\n"
                && is_array($openedFinalFlag)
                && is_array($namedFinalFlag)
                && (int)($openedFinalFlag['dev'] ?? -1) === (int)($finalFlag['dev'] ?? -2)
                && (int)($openedFinalFlag['ino'] ?? -1) === (int)($finalFlag['ino'] ?? -2)
                && (int)($namedFinalFlag['dev'] ?? -1) === (int)($finalFlag['dev'] ?? -2)
                && (int)($namedFinalFlag['ino'] ?? -1) === (int)($finalFlag['ino'] ?? -2)
                && (int)($namedFinalFlag['nlink'] ?? 0) === 1
                && (int)($namedFinalFlag['size'] ?? -1) === 2
            );
            $finalIdentityMatches = (
                $restartAlreadyQueued
                || (
                    is_array($createdFlagIdentity)
                    && is_array($finalFlag)
                    && (int)($finalFlag['dev'] ?? -1) === $createdFlagIdentity['dev']
                    && (int)($finalFlag['ino'] ?? -1) === $createdFlagIdentity['ino']
                )
            );
            $restartOk = (
                ($restartOk || $restartAlreadyQueued)
                && $finalIdentityMatches
                && $finalContentStable
                && is_array($finalFlag)
                && ((((int)$finalFlag['mode']) & 0170000) === 0100000)
                && ((int)($finalFlag['nlink'] ?? 0) === 1)
                && ((int)($finalFlag['size'] ?? -1) === 2)
                && ((((int)$finalFlag['mode']) & 0777) === 0660)
            );
            if (!$restartOk) {
                // Nur den von diesem Request erzeugten, unveränderten Inode
                // entfernen. Ein paralleler Request darf nie gelöscht werden.
                if (is_array($createdFlagIdentity) && is_array($namedFinalFlag)) {
                    if (
                        (int)($namedFinalFlag['dev'] ?? -1) === $createdFlagIdentity['dev']
                        && (int)($namedFinalFlag['ino'] ?? -1) === $createdFlagIdentity['ino']
                    ) {
                        @unlink($restartFlag);
                    }
                }
                echo json_encode([
                    'success' => false,
                    'message' => 'Docker-Neustartflag konnte nicht sicher geschrieben werden.'
                ]);
                exit;
            }
            echo json_encode([
                'success' => true,
                'message' => 'Docker-Container wird über den überwachten PID-1-Dienstsatz neu gestartet.'
            ]);
        } else {
            $services = [
                'e3dc-live',
                'energy_manager',
                'e3dc-wallbox-manager',
                'e3dc-ha',
                'e3dc-epex-manager',
                'e3dc-notifier',
                'e3dc-matter-bridge',
                'e3dc-storage-manager',
                'e3dc-storage-simulator',
                'e3dc-weather-manager'
            ];

            $restartResult = e3dcRunServiceWrapperAction('restart', $services);
            $restarted = $restartResult['changed'] ?? [];
            $ignored = $restartResult['ignored'] ?? [];
            $errors = $restartResult['errors'] ?? [];

            if (!$errors) {
                echo json_encode([
                    'success' => true,
                    'message' => 'Dienste neu gestartet: ' . implode(', ', $restarted)
                ]);
            } else {
                echo json_encode([
                    'success' => false,
                    'message' => "Fehler beim Neustart:\n" . implode("\n", $errors)
                ]);
            }
        }
        exit;
    }
}

/**
 * Führt den Rechte-Check und die automatische Korrektur per Python aus
 */
function handleFixPermissions() {
    if (isset($_GET['action']) && $_GET['action'] === 'fix_permissions') {
        e3dcRequirePostMutation(true);
        header('Content-Type: application/json');
        if (!e3dcPrivilegedInstallerWebActionsEnabled()) {
            echo json_encode([
                'success' => false,
                'message' => e3dcPrivilegedInstallerWebBlockMessage('Rechte-Reparatur'),
            ]);
            exit;
        }

        $paths = getInstallPaths();
        if (empty($paths['valid'])) {
            echo json_encode(['success' => false, 'message' => $paths['error'] ?? 'Installationskontext fehlt.']);
            exit;
        }
        $candidates = [
            rtrim($paths['install_path'], '/') . '/installer_main.py',
            rtrim($paths['install_path'], '/') . '/Installer/self_update.py'
        ];
        $installerScript = false;
        foreach ($candidates as $c) {
            if (file_exists($c)) { $installerScript = $c; break; }
        }
        if (!$installerScript) {
            echo json_encode(['success' => false, 'message' => 'Updater nicht gefunden.']);
            exit;
        }
        $repoDir = (basename($installerScript) === 'installer_main.py') ? dirname($installerScript) : dirname(dirname($installerScript));
        $installerWrapper = $repoDir . '/Installer/installer_wrapper.sh';
        $wrapperInspection = e3dcInspectInstallerWrapper($installerWrapper);
        $attempts = [];
        if (basename($installerScript) === 'installer_main.py' && !empty($wrapperInspection['ok'])) {
            $attempts[] = [
                'label' => 'installer_wrapper.sh',
                'cmd' => "sudo -n " . escapeshellarg($installerWrapper) . " fix_permissions",
            ];
        }
        $attempts[] = [
            'label' => 'installer_main.py direkt',
            'cmd' => "sudo -n /usr/bin/python3 " . escapeshellarg($installerScript) . " --fix-permissions",
        ];

        $cmd = '';
        $out = [];
        $ret = 1;
        $failedAttempts = [];
        foreach ($attempts as $attempt) {
            $attemptOut = [];
            exec($attempt['cmd'] . " 2>&1", $attemptOut, $attemptRet);
            if ($attemptRet === 0) {
                $cmd = $attempt['cmd'];
                $out = $attemptOut;
                $ret = 0;
                break;
            }
            $failedAttempts[] = $attempt['label'] . " fehlgeschlagen:\n" . implode("\n", $attemptOut);
            $cmd = $attempt['cmd'];
            $out = $attemptOut;
            $ret = $attemptRet;
        }

        if ($ret === 0) {
            $filtered = [];
            foreach ($out as $line) {
                // ANSI Colors entfernen
                $clean_line = preg_replace('/\e\[[0-9;]*m/', '', $line);
                $trim_line = trim($clean_line);

                // Wir filtern alles heraus, was auf Erfolg oder reine Status-Info hindeutet
                if ($trim_line === '') continue;
                if (strpos($trim_line, '✓') !== false) continue;
                if (strpos($trim_line, ' OK') !== false) continue;
                if (strpos($trim_line, '===') === 0) continue;
                if (strpos($trim_line, '---') === 0) continue;
                if (strpos($trim_line, 'Prüfe ') === 0) continue;

                $filtered[] = $clean_line; // Wir behalten das Original (clean_line wg. Einrückungen)
            }

            // Wenn nach dem Filtern nur noch "→ Korrigiere..." Action-Header übrig bleiben, war alles OK
            $has_real_issues = false;
            foreach ($filtered as $f) {
                if (strpos($f, '→ Korrigiere') === false && strpos($f, '■ Bereinige') === false) {
                    $has_real_issues = true;
                    break;
                }
            }

            if (!$has_real_issues) {
                $filtered = ["\nAlles OK! Es waren keine Reparaturen notwendig."];
            }
            echo json_encode(['success' => true, 'message' => implode("\n", $filtered)]);
        } else {
            $debug = [];
            exec("whoami", $debug);
            exec("ls -la " . escapeshellarg($installerScript) . " 2>&1", $debug);
            $consoleCmd = "cd " . escapeshellarg($repoDir) . " && sudo python3 installer_main.py --fix-permissions";
            $sudoText = implode("\n", array_filter([
                implode("\n\n", $failedAttempts),
                implode("\n", $out),
            ]));
            if (empty($wrapperInspection['ok'])) {
                $err_msg = e3dcInstallerPrivilegeFailureMessage(
                    'Rechte-Reparatur',
                    $repoDir,
                    $wrapperInspection,
                    [$sudoText]
                );
            } elseif (preg_match('/sudo:|password|not in the sudoers|terminal is required/i', $sudoText)) {
                $err_msg = "Die WebUI darf die Rechte-Reparatur noch nicht per sudo starten.\n\n"
                         . "Bitte einmal per SSH ausführen:\n" . $consoleCmd . "\n\n"
                         . "Danach Apache neu laden und die Seite aktualisieren.\n\n"
                         . "Antwort:\n" . $sudoText;
            } else {
                $err_msg = "Rechte-Reparatur fehlgeschlagen.\n\nVersuchter Befehl:\n" . $cmd . "\n\nAntwort:\n" . $sudoText . "\n\nDEBUG-INFO:\n" . implode("\n", $debug);
            }
            echo json_encode(['success' => false, 'message' => $err_msg]);
        }
        exit;
    }
}

/**
 * Prüft den Status des Watchdog-Services (piguard).
 * Liefert JSON zurück: {installed: bool, active: bool, warning: bool, message: string}
 */
function handleWatchdogStatus() {
    if (isset($_GET['action']) && $_GET['action'] === 'watchdog_status') {
        header('Content-Type: application/json');

        $scriptPath = '/usr/local/bin/pi_guard.sh';
        $heartbeat = '/var/www/html/ramdisk/watchdog.heartbeat';
        if (!file_exists($scriptPath) && !file_exists($heartbeat)) {
            echo json_encode(['installed' => false]);
            exit;
        }

        $warning = false;
        $isActive = false;

        if (file_exists($heartbeat)) {
            $hbData = explode(';', trim(file_get_contents($heartbeat)));
            $age = time() - (int)$hbData[0];
            $isActive = ($age < 60);
            $warning = (isset($hbData[1]) && $hbData[1] === 'WARNING');
        } elseif (file_exists($scriptPath)) {
            exec("systemctl is-active piguard 2>&1", $out, $ret);
            $isActive = (trim(implode('', $out)) === 'active');
        }

        $message = $isActive ? 'Watchdog aktiv' : 'Watchdog inaktiv';
        if ($warning) $message = 'Watchdog warnt vor Fehlern!';

        // Warnung prüfen (Datei-Alter)
        // Wir lesen MONITOR_FILE aus dem Skript, um die Logik synchron zu halten
        $content = file_exists($scriptPath) ? @file_get_contents($scriptPath) : false;
        if (!$warning && $content && preg_match('/MONITOR_FILE="([^"]*)"/', $content, $m)) {
            $monFile = trim($m[1]);
            if ($monFile) {
                // Platzhalter {{day}} auflösen (wie im Bash-Skript)
                if (strpos($monFile, '{{day}}') !== false) {
                    $pattern = str_replace('{{day}}', '*', $monFile);
                    $files = glob($pattern);
                    if ($files) {
                        // Neueste Datei zuerst (analog zu ls -t)
                        usort($files, function($a, $b) { return filemtime($b) - filemtime($a); });
                        $monFile = $files[0];
                    } else {
                        // Fallback auf Wochentag
                        $days = [1=>'Mo', 2=>'Di', 3=>'Mi', 4=>'Do', 5=>'Fr', 6=>'Sa', 7=>'So'];
                        $monFile = str_replace('{{day}}', $days[date('N')], $monFile);
                    }
                }

                if (file_exists($monFile)) {
                    $age = time() - filemtime($monFile);
                    if ($age > 900) { // > 15 Min (900 Sek)
                        $warning = true;
                        $min = floor($age / 60);
                        $message = "Warnung: Protokoll seit {$min} Min. nicht aktualisiert!";
                    }
                }
            }
        }

        // --- NEU: Diagnose Fehler finden (Delta-Check) ---
        $errorLogs = [];
        $ackFile = '/var/www/html/ramdisk/diagnose_ack.json';
        $ackState = file_exists($ackFile) ? @json_decode(file_get_contents($ackFile), true) : null;
        if (!is_array($ackState)) $ackState = ['time' => 0, 'sizes' => []];

        $confData = loadE3dcConfig();
        $c = $confData['config'] ?? [];
        $isDocker = file_exists('/.dockerenv');
        $isLux = (isset($c['luxtronik']) && in_array(strtolower(trim($c['luxtronik'])), ['1', 'true']));
        $isEM = $isLux || (isset($c['auto_mode']) && in_array(strtolower(trim($c['auto_mode'])), ['1', 'true'])) || (isset($c['morning_boost_enable']) && in_array(strtolower(trim($c['morning_boost_enable'])), ['1', 'true']));
        $isBlue = !empty($c['bluelink_refresh_token']);
        $isMqtt = !empty($c['mqtt_hub_ip']) && $c['mqtt_hub_ip'] !== '0.0.0.0';
        $isHa = (isset($c['ha_mode']) && !in_array(strtolower(trim($c['ha_mode'])), ['off', '']));

        $logFiles = [
            'notifier'       => '/var/www/html/logs/notification_manager.log',
            'websocket'      => '/var/www/html/logs/e3dc_websocket.log',
        ];
        if ($isEM) $logFiles['energy_manager'] = '/var/www/html/logs/energy_manager.log';
        if ($isHa) $logFiles['ha_manager'] = '/var/www/html/logs/ha_manager.log';
        if ($isBlue) $logFiles['bluelink'] = '/var/www/html/logs/bluelink_client.log';
        if ($isMqtt) $logFiles['mqtt_hub'] = '/var/www/html/logs/e3dc_mqtt_hub.log';
        if (!$isDocker) {
            $logFiles['watchdog'] = '/var/www/html/logs/piguard.log';
            $logFiles['update'] = '/var/www/html/logs/update.log';
            $logFiles['self_update'] = '/var/log/e3dc-control/web-update.log';
        }

        foreach ($logFiles as $key => $file) {
            $lastSize = $ackState['sizes'][$key] ?? 0;
            if (file_exists($file)) {
                $currSize = filesize($file);
                $newContent = "";
                if ($currSize > $lastSize) {
                    $f = @fopen($file, 'r');
                    if ($f) { fseek($f, $lastSize); $newContent = fread($f, min($currSize - $lastSize, 100000)); fclose($f); }
                } elseif ($currSize < $lastSize && $currSize > 0) { // Datei wurde durch Log-Rotation verkleinert
                    $f = @fopen($file, 'r');
                    if ($f) { $newContent = fread($f, min($currSize, 100000)); fclose($f); }
                }
                $hasError = false;
                if ($newContent && preg_match('/\b(error|exception|critical|traceback|failed)\b/i', $newContent)) {
                    $hasError = true;
                    // Sonderregel für Update-Skripte: Ignoriere harmlose "error" (z.B. Rechte-Warnings), solange keine echten Exceptions fliegen.
                    if (in_array($key, ['update', 'self_update']) && !preg_match('/\b(exception|critical|traceback)\b/i', $newContent)) {
                        $hasError = false;
                    }
                }
                if ($hasError) {
                    $errorLogs[] = $key;
                }
            }
        }

        if (!file_exists('/.dockerenv')) {
            $journalServices = ['watchdog' => ['type' => 'tag', 'name' => 'PIGUARD'], 'notifier' => ['type' => 'unit', 'name' => 'e3dc-notifier'], 'bluelink' => ['type' => 'unit', 'name' => 'e3dc-bluelink'], 'websocket' => ['type' => 'unit', 'name' => 'e3dc-websocket']];
            $since = !empty($ackState['time']) ? '@' . $ackState['time'] : '1 day ago';
            foreach ($journalServices as $key => $j) {
                if (in_array($key, $errorLogs)) continue;
                $cmd = ($j['type'] === 'tag') ? "journalctl -t " . escapeshellarg($j['name']) . " --since=" . escapeshellarg($since) . " -p 3 -n 1 --no-pager 2>/dev/null" : "journalctl -u " . escapeshellarg($j['name']) . " --since=" . escapeshellarg($since) . " -p 3 -n 1 --no-pager 2>/dev/null";
                $out = shell_exec($cmd);
                if (trim($out) && strpos($out, '-- No entries --') === false) $errorLogs[] = $key;
            }
        }

        echo json_encode([
            'installed' => true,
            'active' => $isActive,
            'warning' => $warning,
            'message' => $message,
            'diagnose_errors' => $errorLogs
        ]);
        exit;
    }
}

/**
 * Liefert das Watchdog-Log (journalctl) zurück.
 */
function handleWatchdogLog() {
    if (isset($_GET['action']) && $_GET['action'] === 'watchdog_log') {
        requireWebAuth(true);
        header('Content-Type: text/plain; charset=utf-8');
        // Letzte 50 Einträge, neueste zuerst
            $logFile = '/var/www/html/logs/piguard.log';
            if (file_exists($logFile)) {
                $lines = e3dcReadTextTailLines($logFile, 50, 512 * 1024, true);
                if ($lines) {
                    $text = implode("\n", array_reverse($lines));
                    // Repariere veraltete ISO-8859-1 Umlaute (z.B. "Mär") aus alten Logs
                    echo str_replace("\xE4", "ä", $text);
                }
            } else {
                passthru("journalctl -t PIGUARD -n 50 --no-pager --reverse 2>&1");
            }
        exit;
    }
}

/**
 * Liefert das Log des Energy Managers (AJAX).
 */
function handleEnergyManagerLog() {
    if (isset($_GET['action']) && $_GET['action'] === 'get_energy_manager_log') {
        requireWebAuth(true);
        header('Content-Type: text/plain; charset=utf-8');
        $logFile = '/var/www/html/logs/energy_manager.log';
        if (file_exists($logFile) && is_readable($logFile)) {
            $lines = e3dcReadTextTailLines($logFile, 150, 1024 * 1024);
            // Letzte 150 Zeilen für bessere Übersicht
            $last_lines = $lines;
            echo "--- Letzte 150 Einträge aus energy_manager.log ---\n\n";
            echo implode("\n", $last_lines);
        } else {
            echo "Log-Datei nicht gefunden oder nicht lesbar unter: " . htmlspecialchars($logFile);
        }
        exit;
    }
}
/**
 * Erzeugt das HTML für das Verbindungs-Badge (Online/Offline).
 * Einheitlich für Desktop und Mobile.
 */
function renderConnectionBadge() {
    return '<span id="ha-badge" class="badge bg-secondary rounded-pill me-1" style="display:none; cursor:pointer;" onclick="showHALog()" title="HA/Shadow Status (Klick für Log)">HA</span>' .
           '<span id="connection-status" class="badge bg-secondary rounded-pill" style="cursor:pointer;" onclick="handleConnectionClick()" title="Status: Klicken zum Aktualisieren">Verbinde...</span>';
}

/**
 * Generiert das HTML für das HA-Log Modal.
 */
function renderHAModal($dialogClass = 'modal-lg modal-dialog-scrollable') {
    return '
    <div class="modal fade" id="haModal" tabindex="-1" aria-hidden="true">
        <div class="modal-dialog ' . $dialogClass . '">
            <div class="modal-content bg-body-secondary text-body border-secondary">
                <div class="modal-header border-secondary">
                    <h5 class="modal-title"><i class="fas fa-server me-2"></i>High Availability Log</h5>
                    <button type="button" class="btn-close e3dc-modal-close" data-bs-dismiss="modal" aria-label="Close"></button>
                </div>
                <div class="modal-body e3dc-log-body p-2">
                    <pre id="ha-log-content" class="e3dc-log-pre">Lade...</pre>
                </div>
                <div class="modal-footer border-secondary">
                    <button type="button" class="btn btn-secondary w-100" data-bs-dismiss="modal">Schließen</button>
                </div>
            </div>
        </div>
    </div>';
}

/**
 * Prüft auf Versions-Anfrage (?check_version) und gibt den Zeitstempel zurück.
 * Beendet das Skript, falls zutreffend.
 */
function handleVersionCheck($file) {
    if (isset($_GET['check_version'])) {
        header('Content-Type: text/plain');
        echo filemtime($file);
        exit;
    }
}

/**
 * Generiert das HTML für das Watchdog-Protokoll Modal.
 */
function renderWatchdogModal($dialogClass = 'modal-lg modal-dialog-scrollable') {
    return renderE3dcModalThemeStyles() . '
    <div class="modal fade" id="watchdogModal" tabindex="-1" aria-hidden="true">
        <div class="modal-dialog ' . $dialogClass . '">
            <div class="modal-content bg-body-secondary text-body border-secondary">
                <div class="modal-header border-secondary">
                    <h5 class="modal-title"><i class="fas fa-shield-alt me-2"></i>Watchdog Protokoll</h5>
                    <button type="button" class="btn-close e3dc-modal-close" data-bs-dismiss="modal" aria-label="Close"></button>
                </div>
                <div class="modal-body e3dc-log-body p-2">
                    <pre id="watchdog-log-content" class="e3dc-log-pre">Lade...</pre>
                </div>
                <div class="modal-footer border-secondary">
                    <button type="button" class="btn btn-secondary w-100" data-bs-dismiss="modal">Schließen</button>
                </div>
            </div>
        </div>
    </div>';
}

/**
 * Generiert das HTML für das System-Update Modal.
 */
function renderUpdateModal($dialogClass = 'modal-lg modal-dialog-scrollable') {
    return renderE3dcModalThemeStyles() . '
    <div class="modal fade" id="updateModal" tabindex="-1" data-bs-backdrop="static" data-bs-keyboard="false">
        <div class="modal-dialog ' . $dialogClass . '">
            <div class="modal-content bg-body-secondary text-body border-secondary">
                <div class="modal-header border-secondary">
                    <h5 class="modal-title"><i class="fas fa-sync fa-spin me-2" id="update-spinner"></i><span id="update-modal-title">System Update</span></h5>
                    <button type="button" class="btn-close e3dc-modal-close" data-bs-dismiss="modal" aria-label="Close" id="update-close-btn" style="display:none;"></button>
                </div>
                <div class="modal-body e3dc-log-body p-2">
                    <pre id="update-log" class="e3dc-log-pre e3dc-log-terminal">Starte Update-Prozess...</pre>
                </div>
                <div class="modal-footer border-secondary">
                    <button type="button" class="btn btn-secondary w-100" data-bs-dismiss="modal" id="update-finish-btn" disabled>Schließen</button>
                </div>
            </div>
        </div>
    </div>';
}

/**
 * Generiert das HTML für das Changelog Modal.
 */
function renderReleaseRollbackModal($dialogClass = 'modal-lg modal-dialog-scrollable') {
    return renderE3dcModalThemeStyles() . '
    <div class="modal fade" id="releaseRollbackModal" tabindex="-1" aria-hidden="true">
        <div class="modal-dialog ' . $dialogClass . '">
            <div class="modal-content bg-body-secondary text-body border-secondary">
                <div class="modal-header border-secondary">
                    <h5 class="modal-title"><i class="fas fa-life-ring me-2 text-warning"></i>Rückfallversion</h5>
                    <button type="button" class="btn-close e3dc-modal-close" data-bs-dismiss="modal" aria-label="Close"></button>
                </div>
                <div class="modal-body">
                    <div class="d-flex flex-wrap gap-2 align-items-center mb-3">
                        <span class="badge bg-secondary" id="rollback-current-version">Aktuell: --</span>
                        <span class="badge bg-success" id="rollback-stable-version">Stable: --</span>
                        <span class="badge bg-info text-dark" id="rollback-environment">--</span>
                    </div>
                    <label for="rollback-release-select" class="form-label small text-muted">Version wählen</label>
                    <select id="rollback-release-select" class="form-select mb-3"></select>
                    <div id="rollback-warning" class="alert alert-warning py-2 small mb-3">Release-Liste wird geladen...</div>
                    <pre id="rollback-command-preview" class="e3dc-log-pre e3dc-log-terminal p-2 border border-secondary rounded" style="min-height:160px;">Lade...</pre>
                    <pre id="rollback-run-log" class="e3dc-log-pre e3dc-log-terminal p-2 border border-secondary rounded mt-3" style="display:none;min-height:260px;"></pre>
                </div>
                <div class="modal-footer border-secondary d-flex gap-2">
                    <button type="button" class="btn btn-outline-secondary" id="rollback-copy-btn" onclick="copyReleaseRollbackCommands()"><i class="fas fa-copy me-2"></i>Befehle kopieren</button>
                    <button type="button" class="btn btn-warning" id="rollback-run-btn" onclick="startReleaseRollback()"><i class="fas fa-rotate-left me-2"></i>Rückfall installieren</button>
                    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Schließen</button>
                </div>
            </div>
        </div>
    </div>';
}

function renderChangelogModal($dialogClass = 'modal-lg modal-dialog-scrollable') {
    $logFile = 'CHANGELOG.md';
    $content = "Kein Changelog verfügbar.";
    if (file_exists($logFile)) {
        $content = htmlspecialchars(file_get_contents($logFile));
    }

    return '
    <div class="modal fade" id="changelogModal" tabindex="-1" aria-hidden="true">
        <div class="modal-dialog ' . $dialogClass . '">
            <div class="modal-content bg-body-secondary text-body border-secondary">
                <div class="modal-header border-secondary">
                    <h5 class="modal-title">Changelog</h5>
                    <button type="button" class="btn-close e3dc-modal-close" data-bs-dismiss="modal" aria-label="Close"></button>
                </div>
                <div class="modal-body">
                    <pre class="e3dc-changelog-pre" style="font-size:0.85rem;">' . $content . '</pre>
                </div>
            </div>
        </div>
    </div>';
}

/**
 * Generiert das HTML für das zentrale Diagnose & Log Modal.
 */
function renderDiagnoseModal($dialogClass = 'modal-lg modal-dialog-scrollable') {
    $confData = loadE3dcConfig();
    $c = $confData['config'] ?? [];
    $isDocker = file_exists('/.dockerenv');
    $isLux = (isset($c['luxtronik']) && in_array(strtolower(trim($c['luxtronik'])), ['1', 'true']));
    $isEM = $isLux || (isset($c['auto_mode']) && in_array(strtolower(trim($c['auto_mode'])), ['1', 'true'])) || (isset($c['morning_boost_enable']) && in_array(strtolower(trim($c['morning_boost_enable'])), ['1', 'true']));
    $isBlue = !empty($c['bluelink_refresh_token']);
    $isMqtt = !empty($c['mqtt_hub_ip']) && $c['mqtt_hub_ip'] !== '0.0.0.0';
    $isHa = (isset($c['ha_mode']) && !in_array(strtolower(trim($c['ha_mode'])), ['off', '']));

    $html = renderE3dcModalThemeStyles() . '
    <div class="modal fade" id="diagnoseModal" tabindex="-1" aria-hidden="true">
        <div class="modal-dialog ' . $dialogClass . '">
            <div class="modal-content bg-body-secondary text-body border-secondary">
                <div class="modal-header border-secondary d-flex flex-column align-items-start">
                    <div class="d-flex justify-content-between w-100 align-items-center mb-2">
                        <h5 class="modal-title"><i class="fas fa-stethoscope me-2 text-info"></i>System Diagnose & Logs</h5>
                        <div class="d-flex align-items-center gap-3">
                            <button type="button" class="btn btn-sm btn-outline-warning rounded-pill px-3 fw-bold" onclick="ackDiagnose()" id="btn-ack-diagnose" style="display:none;"><i class="fas fa-check"></i> Quittieren</button>
                            <button type="button" class="btn-close e3dc-modal-close" data-bs-dismiss="modal" aria-label="Close"></button>
                        </div>
                    </div>
                    <select class="form-select form-select-sm bg-body text-body border-secondary w-100" id="diagnoseLogSelect" onchange="loadDiagnoseLog()">';

    if ($isEM) $html .= '<option value="energy_manager">Energy Manager</option>';
    $html .= '<option value="wallbox_manager">Native Wallbox Manager</option>';
    if ($isLux) $html .= '<option value="wp_status">Wärmepumpe Status & Fehler</option>';
    $html .= '<option value="notifier">Notification & Schedule Manager</option>';
    $html .= '<option value="wallbox_command_gate">Wallbox Command-Gate</option>';
    if ($isBlue) $html .= '<option value="bluelink">Bluelink (Fahrzeug)</option>';
    if ($isMqtt) $html .= '<option value="mqtt_hub">Smart Home MQTT Hub</option>';
    $html .= '<option value="websocket">WebSocket Server</option>';
    if (!$isDocker) $html .= '<option value="watchdog">Watchdog (PiGuard)</option>';
    if ($isHa) $html .= '<option value="ha_manager">High Availability (Cluster)</option>';
    $html .= '<option value="config_validation">Config-Validator</option>';
    $html .= '<option value="storage_decision_history">Speicher-Entscheidungen</option>';
    $html .= '<option value="ems_reaction_history">EMS-Reaktionszeit</option>';
    $html .= '<option value="energy_decision_history">Wärme-Entscheidungen</option>';
    $html .= '<option value="wallbox_decision_history">Wallbox-Entscheidungen</option>';
    $html .= '<option value="storage_manager">Storage Manager (Gehirn Live)</option>';
    $html .= '<option value="storage_simulator">Storage Simulator (Fahrplan)</option>';
    if (!$isDocker) {
        $html .= '<option value="update">Update Log (V4 System)</option>';
        $html .= '<option value="self_update">Web-UI Update Log</option>';
    } else {
        $html .= '<option value="docker">Docker Info</option>';
        $html .= '<option value="watchtower">Watchtower (Docker Updates)</option>';
    }
    if ($isLux) $html .= '<option value="wp_raw">Wärmepumpen-Rohdaten (JSON)</option>';

    $html .= '
                    </select>
                </div>
                <div class="modal-body e3dc-log-body p-2">
                    <pre id="diagnose-log-content" class="e3dc-log-pre">Wähle ein Log aus...</pre>
                </div>
                <div class="modal-footer border-secondary">
                    <button type="button" class="btn btn-outline-info w-100" onclick="loadDiagnoseLog()"><i class="fas fa-sync-alt me-2"></i> Aktualisieren</button>
                </div>
            </div>
        </div>
    </div>
    <script>
    function showDiagnoseModal() { const m = new bootstrap.Modal(document.getElementById("diagnoseModal")); m.show(); updateDiagnoseDropdown(); loadDiagnoseLog(); }
    function showDiagnoseLog(type) {
        const el = document.getElementById("diagnoseModal");
        let m = bootstrap.Modal.getInstance(el);
        if (!m) m = new bootstrap.Modal(el);
        m.show();
        updateDiagnoseDropdown();
        const sel = document.getElementById("diagnoseLogSelect");
        if (sel) { sel.value = type; }
        loadDiagnoseLog();
    }
    function updateDiagnoseDropdown() {
        const sel = document.getElementById("diagnoseLogSelect"); let hasErr = false;
        if (sel && window.currentDiagnoseErrors) {
            Array.from(sel.options).forEach(opt => {
                let txt = opt.text.replace(" ⚠️", "");
                if (window.currentDiagnoseErrors.includes(opt.value)) {
                    opt.text = txt + " ⚠️"; opt.classList.add("text-warning", "fw-bold"); hasErr = true;
                } else {
                    opt.text = txt; opt.classList.remove("text-warning", "fw-bold");
                }
            });
        }
        const ackBtn = document.getElementById("btn-ack-diagnose"); if (ackBtn) ackBtn.style.display = hasErr ? "inline-block" : "none";
    }
    function loadDiagnoseLog() { const t = document.getElementById("diagnoseLogSelect").value; const c = document.getElementById("diagnose-log-content"); c.innerText = "Lade Protokoll..."; fetch("?action=get_system_log&log=" + t).then(r => r.text()).then(txt => c.innerText = txt).catch(() => c.innerText = "Fehler beim Laden."); }
    function ackDiagnose() {
        fetch("?action=ack_diagnose&t=" + Date.now(), {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": String(window.E3DC_CSRF_TOKEN || "")
            }
        }).then(r => r.json()).then(d => {
            if (d.success) {
                window.currentDiagnoseErrors = []; updateDiagnoseDropdown();
                document.querySelectorAll(".btn-diagnose").forEach(b => { if (b.dataset.origClass) b.className = b.dataset.origClass; });
            }
        });
    }
    </script>';

    return $html;
}

/**
 * Lädt Preisdaten aus einer statischen Datei (z.B. awattardebug.23.txt),
 * um einen stabilen Tagesgraphen zu ermöglichen.
 */
function loadStaticPriceData($vat = 0) {
    $paths = getInstallPaths();
    $basePath = $paths['install_path'];

    // Zeitabhängige Priorisierung:
    // 18:00 - 06:00: Bevorzuge Mittags-Datei (11/12/13 Uhr), um Vorschau auf morgen zu haben.
    // 06:00 - 18:00: Bevorzuge Nacht-Datei (23/00 Uhr) für stabilen Tagesverlauf.
    $hour = (int)date('G');
    if ($hour >= 18 || $hour < 6) {
        $candidates = ['awattardebug.13.txt', 'awattardebug.14.txt', 'awattardebug.23.txt', 'awattardebug.0.txt'];
    } else {
        $candidates = ['awattardebug.23.txt', 'awattardebug.0.txt', 'awattardebug.12.txt', 'awattardebug.13.txt'];
    }

    $lines = false;
    $loadedFile = '';

    foreach ($candidates as $f) {
        if (file_exists($basePath . $f)) {
            $lines = @file($basePath . $f, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
            if ($lines === false) {
                // Fallback: Versuch via cat (falls PHP-Leserechte klemmen)
                exec("cat " . escapeshellarg($basePath . $f), $out, $ret);
                if ($ret === 0 && !empty($out)) $lines = $out;
            }

            if ($lines) {
                $loadedFile = $f;
                break;
            }
        }
    }

    if (!$lines) return false;

    $prices = [];
    $startHour = null;
    $interval = null;
    $lastH = null;

    foreach ($lines as $line) {
        // BOM entfernen (UTF-8)
        $line = preg_replace('/^\xEF\xBB\xBF/', '', $line);
        $line = trim($line);

        // Header überspringen
        if (empty($line) || (!is_numeric(substr($line, 0, 1)) && substr($line, 0, 1) !== '-')) {
            if (!empty($prices)) break; // Stop bei neuem Block (z.B. "Data")
            continue;
        }

        // Trenner erkennen (Semikolon oder Whitespace)
        $parts = (strpos($line, ';') !== false) ? explode(';', $line) : preg_split('/\s+/', $line);

        if (count($parts) >= 2 && is_numeric($parts[0])) {
            $rawT = (float)str_replace(',', '.', trim($parts[0]));
            $val = (float)str_replace(',', '.', trim($parts[1]));

            // Timestamp (> 48) zu Stunde (0-23.99) konvertieren
            if ($rawT > 48) {
                $h = (float)gmdate('G', (int)$rawT) + ((int)gmdate('i', (int)$rawT)/60);
            } else {
                $h = $rawT;
            }


            // Duplikat-Check: Wenn wir wieder beim Start sind (z.B. neuer Block)
            if ($startHour !== null && abs($h - $startHour) < 0.001 && count($prices) > 0) break;

            if ($vat > 0) $val = $val * (1 + ($vat / 100));
            $prices[] = $val;

            if ($startHour === null) {
                $startHour = $h;
            } elseif ($interval === null) {
                $diff = $h - $lastH;
                if ($diff < 0) $diff += 24; // Tageswechsel
                if ($diff > 0.001) $interval = $diff;
            }
            $lastH = $h;
        }
    }

    return empty($prices) ? false : [
        'prices' => $prices,
        'start_hour' => $startHour,
        'interval' => ($interval ?: 1),
        'source' => $loadedFile
    ];
}

/**
 * Liest verfügbare History-Backup-Dateien aus dem Backup-Verzeichnis.
 * Liefert ein Array mit 'file' (Dateiname) und 'label' (formatiertes Datum).
 */
function getHistoryBackupFiles($backupDir = '/var/www/html/data/history_backups/') {
    $historyFiles = [];
    if (is_dir($backupDir)) {
        $files = glob($backupDir . 'history_*.txt');
        if ($files) {
            rsort($files); // Neueste zuerst
            foreach ($files as $file) {
                $basename = basename($file);
                if (preg_match('/history_(\d{4}-\d{2}-\d{2})\.txt/', $basename, $m)) {
                    $label = date('d.m.Y', strtotime($m[1]));
                    $historyFiles[] = ['file' => $basename, 'label' => $label];
                }
            }
        }
    }
    return $historyFiles;
}

/**
 * Hilfsfunktion zum Parsen von Kommazahlen aus Config-Dateien.
 */
function parseConfigFloat($val) {
    return (float)str_replace(',', '.', $val);
}

/**
 * Berechnet 24h Mittelwerte aus der History-Datei (Cache 1 Std).
 */
function get24hAverages($filePath) {
    $cacheFile = '/var/www/html/ramdisk/e3dc_avgs.json';
    if (file_exists($cacheFile) && (time() - filemtime($cacheFile)) < 3600) {
        return json_decode(file_get_contents($cacheFile), true);
    }

    $avgs = ['home' => 800, 'grid' => 1000, 'wb' => 4000];
    if (file_exists($filePath)) {
        $lines = @file($filePath);
        if ($lines) {
            $lines = array_slice($lines, -1000); // Letzte Einträge
            $sums = ['h' => 0, 'g' => 0, 'w' => 0]; $c = 0;
            foreach ($lines as $l) {
                $d = json_decode($l, true);
                if ($d) { $sums['h'] += abs($d['home_raw']??0); $sums['g'] += abs($d['grid']??0); $sums['w'] += ($d['wb']??0); $c++; }
            }
            if ($c > 0) $avgs = ['home' => $sums['h']/$c, 'grid' => $sums['g']/$c, 'wb' => $sums['w']/$c];
        }
    }
    file_put_contents($cacheFile, json_encode($avgs));
    @chmod($cacheFile, 0666);
    return $avgs;
}

/**
 * Sucht nach archivierten awattardebug-Dateien.
 */
function getArchivedDebugFiles($basePath) {
    $files = [];
    if (is_dir($basePath)) {
        foreach (glob(rtrim($basePath, '/') . '/awattardebug.*.txt') as $f) {
            if (preg_match('/awattardebug\.(\d+)\.txt$/', $f, $m)) {
                $ts = filemtime($f);
                $files[] = [
                    'file' => basename($f),
                    'ts' => $ts,
                    'label' => date('d.m. H:i', $ts) . " (Run {$m[1]})"
                ];
            }
        }
        usort($files, fn($a, $b) => $b['ts'] <=> $a['ts']);
    }
    return $files;
}

/**
 * Speichert eine Einstellung in der V4-Konfiguration (AJAX).
 */
function handleSaveSetting() {
    if (isset($_POST['action']) && $_POST['action'] === 'save_setting') {
        requireWebAuth(true);
        e3dcRequireCsrfToken(true);
        if (!isset($_POST['key'], $_POST['value'])) exit;

        $key = trim($_POST['key']);
        $val = trim($_POST['value']);

        if (!in_array($key, ['darkmode', 'show_forecast'], true) || !in_array($val, ['0', '1'], true)) {
            http_response_code(400);
            echo 'error';
            exit;
        }

        if (saveE3dcConfigValue($key, $val)) echo "ok";
        else { http_response_code(500); echo "error"; }
        exit;
    }
}

/**
 * Verarbeitet ausschließlich die manuelle Zusatz-WR-Notsperre.
 * Hardwarebefehle bleiben dem Storage Manager vorbehalten.
 */
function handleDirectMarketingDashboardAction() {
    $action = (string)($_POST['action'] ?? '');
    if ($action !== 'set_direct_marketing_aux_inverter_shelly_lock') {
        return;
    }

    requireWebAuth(true);
    e3dcRequireCsrfToken(true);
    header('Content-Type: application/json; charset=utf-8');

    $locked = in_array(strtolower(trim((string)($_POST['locked'] ?? '0'))), ['1', 'true', 'yes', 'on'], true);
    $path = '/var/www/html/data/direct_marketing_aux_inverter_shelly_manual_lock.json';
    if ($locked) {
        $payload = [
            'schema' => 'direct_marketing_aux_inverter_shelly_manual_lock_v1',
            'locked' => true,
            'ts' => time(),
            'source' => 'dashboard',
        ];
        $json = json_encode($payload, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
        $ok = $json !== false;
        $ok = $ok && e3dcWriteJsonAtomic($path, $json);
    } else {
        $ok = !file_exists($path) || @unlink($path);
    }
    if (!$ok) http_response_code(500);
    echo json_encode(['success' => $ok, 'locked' => $locked]);
    exit;
}


/**
 * Führt das System-Update aus (AJAX).
 * Ersetzt run_update.php
 */
function handleRunUpdate() {
    if (isset($_GET['action']) && $_GET['action'] === 'run_update') {
        // Caching verhindern (Wichtig für Cloudflare/Browser)
        header('Cache-Control: no-cache, no-store, must-revalidate');
        header('Pragma: no-cache');
        header('Expires: 0');
        header('Content-Type: application/json');
        $mode = (string)($_GET['mode'] ?? $_POST['mode'] ?? '');
        if ($mode === 'poll') {
            requireWebAuth(true);
        } elseif ($mode === 'start') {
            e3dcRequirePostMutation(true);
        } else {
            http_response_code(400);
            echo json_encode(['status' => 'error', 'running' => false, 'message' => 'Ungültiger Update-Modus.']);
            exit;
        }
        if (e3dcIsDockerEnvironment()) {
            echo json_encode([
                'status' => 'docker',
                'running' => false,
                'message' => e3dcDockerHostUpdateMessage(),
                'commands' => e3dcDockerHostUpdateCommandText(),
            ]);
            exit;
        }
        $logFile = '/var/log/e3dc-control/web-update.log';
        $pidFile = '/run/e3dc-web-update/pid';
        $statusFile = '/run/e3dc-web-update/status';
        if ($mode === 'start') {
            $launcherInspection = e3dcInspectWebUpdateLauncher();
            if (empty($launcherInspection['ok'])) {
                echo json_encode([
                    'status' => 'error',
                    'running' => false,
                    'message' => 'Der root-eigene Web-Update-Launcher ist nicht sicher gebunden ('
                        . (string)($launcherInspection['status'] ?? 'unbekannt') . ').',
                ]);
                exit;
            }
            $output = [];
            $exitCode = 1;
            exec('/usr/bin/sudo -n -- /usr/local/sbin/e3dc-web-update-launcher 2>&1', $output, $exitCode);
            echo json_encode([
                'status' => $exitCode === 0 ? 'started' : 'error',
                'running' => $exitCode === 0,
                'message' => trim(implode("\n", $output)),
            ]);
        } elseif ($mode === 'poll') {
            clearstatcache(true, $logFile);
            $log = '';
            $debugInfo = "";

            if (file_exists($logFile)) {
                $size = filesize($logFile);
                $content = file_get_contents($logFile);

                if ($content === false) {
                    $log = "FEHLER: Log-Datei existiert, kann aber nicht gelesen werden (Rechte?).";
                } elseif (empty($content)) {
                    $log = "Status: Warte auf Start... (Log-Datei ist leer, Größe: $size Bytes)";
                } else {
                    $log = $content;
                }
            } else {
                $log = "Status: Initialisiere... (Log-Datei noch nicht erstellt)";
            }
            $running = false;
            if (file_exists($pidFile)) {
                $pid = (int)trim(file_get_contents($pidFile));
                if (file_exists("/proc/$pid")) $running = true;
            }
            $exitCode = null;
            if (file_exists($statusFile)) {
                $rawStatus = trim((string)file_get_contents($statusFile));
                if ($rawStatus !== '' && preg_match('/^-?\d+$/', $rawStatus)) {
                    $exitCode = (int)$rawStatus;
                }
            }

            // JSON Flags für Robustheit (verhindert Absturz bei Emojis/Sonderzeichen)
            $flags = 0;
            if (defined('JSON_INVALID_UTF8_SUBSTITUTE')) $flags |= JSON_INVALID_UTF8_SUBSTITUTE;
            if (defined('JSON_PARTIAL_OUTPUT_ON_ERROR')) $flags |= JSON_PARTIAL_OUTPUT_ON_ERROR;

            $json = json_encode([
                'running' => $running,
                'log' => $log,
                'exit_code' => $exitCode,
                'completed' => !$running && $exitCode !== null,
                'success' => !$running && $exitCode === 0,
            ], $flags);

            if ($json === false) {
                echo json_encode(['running' => $running, 'log' => "JSON-Fehler: " . json_last_error_msg()]);
            } else {
                echo $json;
            }
        }
        exit;
    }
}

/**
 * Gibt die Tagesstatistik für ein bestimmtes Datum zurück (AJAX)
 */
function handleDailyStats() {
    if (isset($_GET['action']) && $_GET['action'] === 'get_daily_stats') {
        requireWebAuth(true);
        header('Content-Type: application/json');
        $file = $_GET['file'] ?? '';
        if ($file === 'today' || empty($file)) {
            $statsFile = '/var/www/html/ramdisk/daily_stats.json';
            if (file_exists($statsFile)) {
                $statsData = @json_decode((string)@file_get_contents($statsFile), true);
                if (is_array($statsData)) {
                    $confData = loadE3dcConfig();
                    e3dcApplyEegRevenueToDailyStats($statsData, $confData['config'] ?? []);
                    echo json_encode($statsData);
                } else {
                    echo file_get_contents($statsFile);
                }
            }
            else echo json_encode(['error' => 'No live data']);
        } else {
            if (!preg_match('/^history_\d{4}-\d{2}-\d{2}\.txt$/', $file)) { echo json_encode(['error' => 'Invalid']); exit; }
            $path = '/var/www/html/data/history_backups/' . $file;
            if (file_exists($path)) {
                $lines = file($path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
                $statsData = calculateDailyEnergyStats($lines);
                $confData = loadE3dcConfig();
                e3dcApplyEegRevenueToDailyStats($statsData, $confData['config'] ?? []);
                echo json_encode($statsData);
            } else {
                echo json_encode(['error' => 'Not found']);
            }
        }
        exit;
    }
}

// ==================== AWATTAR & PROGNOSE PARSING ====================

/**
 * Konvertiert Config-Werte robust in Float (behandelt Komma/Punkt).
 */
function parseNumericConfigValue($value, $default) {
    if (is_int($value) || is_float($value)) return (float)$value;
    if (!is_string($value)) return (float)$default;
    $cleaned = trim($value, " \t\n\r\0\x0B\"'");
    if ($cleaned === '') return (float)$default;
    $normalized = str_replace(',', '.', $cleaned);
    return is_numeric($normalized) ? (float)$normalized : (float)$default;
}

function e3dcEegConfigString($config, $key, $default = '') {
    if (!is_array($config) || !array_key_exists($key, $config)) return $default;
    $value = $config[$key];
    if (is_array($value) && array_key_exists('value', $value)) {
        $value = $value['value'];
    }
    $value = trim((string)$value);
    return $value !== '' ? $value : $default;
}

function e3dcEegConfigBool($config, $key, $default = false) {
    $value = strtolower(e3dcEegConfigString($config, $key, $default ? '1' : '0'));
    return in_array($value, ['1', 'true', 'yes', 'on', 'ein'], true);
}

function e3dcEegConfigFloat($config, $key, $default = 0.0) {
    $raw = str_replace(',', '.', e3dcEegConfigString($config, $key, ''));
    return is_numeric($raw) ? (float)$raw : (float)$default;
}

function e3dcEegParseDecimal($value) {
    $value = str_replace(["\xc2\xa0", ' '], '', trim((string)$value));
    $value = str_replace(',', '.', $value);
    return is_numeric($value) ? (float)$value : null;
}

function e3dcEegForecastCapacityKwp($config) {
    $total = 0.0;
    foreach (['forecast1', 'forecast2', 'forecast3', 'forecast4', 'forecast5'] as $key) {
        $raw = e3dcEegConfigString($config, $key, '');
        if ($raw === '') continue;
        $parts = preg_split('/[\/;|\s]+/', str_replace(',', '.', $raw));
        $lastNumber = null;
        foreach ($parts as $part) {
            $num = e3dcEegParseDecimal($part);
            if ($num !== null && $num > 0) {
                $lastNumber = $num;
            }
        }
        if ($lastNumber !== null) {
            $total += $lastNumber;
        }
    }
    return $total;
}

function e3dcEegParseTariffTiers($raw) {
    $tiers = [];
    $lines = preg_split('/\r\n|\r|\n/', (string)$raw);
    foreach ($lines as $line) {
        $line = preg_replace('/#.*/', '', trim($line));
        if ($line === '') continue;
        preg_match_all('/-?\d+(?:[,.]\d+)?/', $line, $matches);
        $numbers = array_map('e3dcEegParseDecimal', $matches[0] ?? []);
        $numbers = array_values(array_filter($numbers, function($n) { return $n !== null; }));
        if (count($numbers) >= 2) {
            $limit = (float)$numbers[0];
            $rate = (float)$numbers[1];
        } elseif (count($numbers) === 1 && empty($tiers)) {
            $limit = 0.0;
            $rate = (float)$numbers[0];
        } else {
            continue;
        }
        if ($rate > 0) {
            $tiers[] = ['limit_kwp' => max(0.0, $limit), 'rate_ct' => $rate];
        }
    }
    usort($tiers, function($a, $b) {
        return ($a['limit_kwp'] <=> $b['limit_kwp']);
    });
    return $tiers;
}

function e3dcEegWeightedTariffCt($tiers, $capacityKwp) {
    if (empty($tiers)) return 0.0;
    if (count($tiers) === 1 && (float)$tiers[0]['limit_kwp'] <= 0.0) {
        return (float)$tiers[0]['rate_ct'];
    }
    $maxTier = 0.0;
    foreach ($tiers as $tier) {
        $maxTier = max($maxTier, (float)$tier['limit_kwp']);
    }
    $capacity = $capacityKwp > 0.0 ? $capacityKwp : $maxTier;
    if ($capacity <= 0.0) return 0.0;

    $prev = 0.0;
    $weighted = 0.0;
    foreach ($tiers as $tier) {
        $limit = (float)$tier['limit_kwp'];
        $rate = (float)$tier['rate_ct'];
        $segment = max(0.0, min($capacity, $limit) - $prev);
        if ($segment > 0.0) {
            $weighted += $segment * $rate;
            $prev += $segment;
        }
        if ($prev >= $capacity) break;
    }
    if ($prev < $capacity && !empty($tiers)) {
        $last = $tiers[count($tiers) - 1];
        $weighted += ($capacity - $prev) * (float)$last['rate_ct'];
    }
    return $weighted / $capacity;
}

function e3dcBuildEegRevenueConfig($config) {
    require_once __DIR__ . '/eeg_tariff_tables.php';

    $enabled = e3dcEegConfigBool($config, 'direct_marketing_eeg_enable', false);
    $rateSource = e3dcEegConfigString($config, 'direct_marketing_eeg_rate_source', 'manual');
    $autoRateSource = in_array($rateSource, ['bnetza_archive', 'bnetza_current_2026_02'], true);
    $tiers = $autoRateSource
        ? e3dc_eeg_tariff_tiers_for_config($config)
        : e3dcEegParseTariffTiers(e3dcEegConfigString($config, 'direct_marketing_eeg_tariff_tiers', ''));
    $capacityKwp = e3dcEegForecastCapacityKwp($config);
    $tariffCt = e3dcEegWeightedTariffCt($tiers, $capacityKwp);
    $commissioning = e3dcEegConfigString($config, 'direct_marketing_eeg_commissioning_date', '');
    $supportYears = max(0, (int)round(e3dcEegConfigFloat($config, 'direct_marketing_eeg_support_years', 20)));
    $supportUntil = '';
    $inSupport = true;

    if ($commissioning !== '' && preg_match('/^\d{4}-\d{2}-\d{2}$/', $commissioning)) {
        $year = (int)substr($commissioning, 0, 4);
        $supportUntil = sprintf('%04d-12-31', $year + $supportYears);
        $end = DateTimeImmutable::createFromFormat('Y-m-d', $supportUntil) ?: null;
        $inSupport = $end ? (new DateTimeImmutable('today') <= $end) : true;
    }

    return [
        'enabled' => $enabled,
        'capacity_kwp' => round($capacityKwp, 3),
        'tariff_ct' => round($tariffCt, 4),
        'rate_source' => $rateSource,
        'commissioning_date' => $commissioning,
        'support_years' => $supportYears,
        'support_until' => $supportUntil,
        'in_support' => $enabled && $tariffCt > 0.0 && $inSupport,
    ];
}

function e3dcApplyEegRevenueToDailyStats(&$dailyStats, $config) {
    if (!is_array($dailyStats)) return;
    if (!isset($dailyStats['costs']) || !is_array($dailyStats['costs'])) $dailyStats['costs'] = [];
    $eeg = e3dcBuildEegRevenueConfig(is_array($config) ? $config : []);
    $gridOutKwh = 0.0;
    if (isset($dailyStats['stats']) && is_array($dailyStats['stats']) && isset($dailyStats['stats']['total_grid_out_kwh'])) {
        $gridOutKwh = max(0.0, (float)$dailyStats['stats']['total_grid_out_kwh']);
    }
    $revenue = !empty($eeg['in_support']) ? ($gridOutKwh * (float)$eeg['tariff_ct'] / 100.0) : 0.0;
    $costTotal = (float)($dailyStats['costs']['total'] ?? 0.0);
    $saveTotal = (float)($dailyStats['costs']['save_total'] ?? 0.0);

    $dailyStats['costs']['eeg_enabled'] = !empty($eeg['enabled']);
    $dailyStats['costs']['eeg_in_support'] = !empty($eeg['in_support']);
    $dailyStats['costs']['eeg_tariff_ct'] = round((float)$eeg['tariff_ct'], 4);
    $dailyStats['costs']['eeg_grid_out_kwh'] = round($gridOutKwh, 2);
    $dailyStats['costs']['eeg_revenue'] = round($revenue, 2);
    $dailyStats['costs']['eeg_net_total'] = round($costTotal - $revenue, 2);
    $dailyStats['costs']['result_total'] = round($saveTotal + $revenue - $costTotal, 2);
}

/**
 * Berechnet den Endpreis basierend auf Rohdaten und Zeitstempel (Formatwechsel 19.12.2024).
 */
function calculateAwattarPrice($priceRaw, $sourceTimestamp, $awmwst, $awnebenkosten) {
    $multiplier = ($awmwst / 100.0) + 1.0;
    $switchTs = strtotime('2024-12-19 00:00:00');
    if ($sourceTimestamp > $switchTs) {
        return ($priceRaw * $multiplier) + $awnebenkosten;
    }
    return (($priceRaw / 10.0) * $multiplier) + $awnebenkosten;
}

/**
 * Klassifiziert einen Preis (günstig/teuer) relativ zu Min/Max.
 */
function classifyPriceLevel($price, $minPrice, $maxPrice) {
    if ($price === null || $minPrice === null || $maxPrice === null || $maxPrice <= $minPrice) return 'unknown';
    $range = $maxPrice - $minPrice;
    $avgBandWidth = 0.30 * $range;
    $lowerThreshold = $minPrice + (($range - $avgBandWidth) / 2.0);
    $upperThreshold = $maxPrice - (($range - $avgBandWidth) / 2.0);
    if ($price < $lowerThreshold) return 'cheap';
    if ($price > $upperThreshold) return 'expensive';
    return 'average';
}

/**
 * Hilfsfunktion: Wandelt "12.45" (Viertelstunden) in Minuten des Tages um.
 */
function parseQuarterTimeToMinute($timeToken) {
    if (!preg_match('/^(\d{1,2})\.(\d{2})$/', $timeToken, $tm)) return null;
    $hour = (int)$tm[1];
    $fractionPart = (int)$tm[2] / 100.0;
    $minute = (int)round($fractionPart * 60.0);
    if ($minute >= 60) { $hour += (int)floor($minute / 60); $minute = $minute % 60; }
    return ($hour * 60) + $minute;
}

/**
 * Hilfsfunktion: Formatiert Minuten des Tages zurück in "12.45" Format.
 */
function minuteToSlotLabel($minute) {
    if (!is_int($minute) || $minute < 0) return null;
    $hour = (int)floor($minute / 60);
    $min = $minute % 60;
    return sprintf('%d.%02d', $hour, (int)round(($min / 60) * 100));
}

/**
 * Parst die awattardebug.txt und extrahiert Preise sowie Prognosedaten.
 */
function parsePricesFromAwattarDebug($debugFile, $awmwst, $awnebenkosten, $speichergroesse) {
    if (!file_exists($debugFile)) return [null, null, null, null, null, null, null, null, null, [], null, 1.0, []];

    $lines = @file($debugFile, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    if (!$lines) return [null, null, null, null, null, null, null, null, null, [], null, 1.0, []];

    $prices = [];
    $forecast = [];
    $entries = [];
    $sourceTs = filemtime($debugFile) ?: time();
    $inDataBlock = false;
    $lastMinute = -1;
    $dayOffset = 0;
    $priceStartHour = null;
    $priceInterval = 1.0;

    foreach ($lines as $line) {
        $line = trim($line);
        if ($line === '') continue;
        if (stripos($line, 'Data') === 0) { $inDataBlock = true; continue; }
        if (stripos($line, 'Simulation') === 0) { $inDataBlock = false; continue; }
        if (stripos($line, 'DV') === 0) { $inDataBlock = false; continue; }
        if (!$inDataBlock) continue;

        if (preg_match('/^\d{1,2}\.\d{2}\s+(-?\d+(?:\.\d+)?)(?:\s+(-?\d+(?:\.\d+)?)){3,5}$/', $line)) {
            $parts = preg_split('/\s+/', $line);
            if (count($parts) >= 2) {
                $minuteOfDay = parseQuarterTimeToMinute($parts[0]);
                if ($minuteOfDay !== null) {
                    if ($lastMinute !== -1 && $minuteOfDay < $lastMinute) $dayOffset += 1440;
                    $lastMinute = $minuteOfDay;
                    $minuteOfDay += $dayOffset;
                }
                $candidateRaw = (float)$parts[1];
                $candidate = calculateAwattarPrice($candidateRaw, $sourceTs, $awmwst, $awnebenkosten);

                if ($candidate >= 0 && $candidate <= 100) {
                    if ($priceStartHour === null) $priceStartHour = (float)$parts[0];
                    elseif (count($prices) === 1) {
                        $iv = (float)$parts[0] - $priceStartHour;
                        if ($iv > 0) $priceInterval = $iv;
                    }
                    $prices[] = $candidate;
                    if ($minuteOfDay !== null) $entries[] = ['minute' => $minuteOfDay, 'price' => $candidate];

                    // Prognose (Spalte 4)
                    if (count($parts) >= 5) {
                        $pvRaw = (float)$parts[4];
                        $pvVal = $pvRaw * $speichergroesse * 40;
                        $h = (float)$parts[0] + ($dayOffset / 60.0);
                        $forecast[] = ['h' => $h, 'w' => $pvVal];
                    }
                }
            }
        }
    }

    if (empty($prices)) return [null, null, null, null, null, null, null, null, null, [], null, 1.0, []];

    $current = $prices[0];
    $selectedMinute = null; $targetMinute = null;
    if (!empty($entries)) {
        $nowMinuteRaw = ((int)gmdate('G') * 60) + (int)gmdate('i');
        $targetMinute = (int)(round($nowMinuteRaw / 15) * 15);
        if ($targetMinute >= 1440) $targetMinute -= 1440;
        $bestEntry = $entries[0];
        $bestDist = abs(($bestEntry['minute'] % 1440) - $targetMinute);
        $bestDist = min($bestDist, 1440 - $bestDist);
        foreach ($entries as $entry) {
            $dist = abs(($entry['minute'] % 1440) - $targetMinute);
            $dist = min($dist, 1440 - $dist);
            if ($dist < $bestDist) { $bestDist = $dist; $bestEntry = $entry; }
        }
        $current = $bestEntry['price']; $selectedMinute = $bestEntry['minute'];
    }
    $min = min($prices); $max = max($prices);
    $minSlot = null; $maxSlot = null;
    foreach ($entries as $entry) {
        if ($entry['price'] === $min && $minSlot === null) $minSlot = minuteToSlotLabel($entry['minute']);
        if ($entry['price'] === $max && $maxSlot === null) $maxSlot = minuteToSlotLabel($entry['minute']);
    }
    return [$current, $min, $max, minuteToSlotLabel($selectedMinute), minuteToSlotLabel($targetMinute), $min, $minSlot, $max, $maxSlot, $prices, $priceStartHour, $priceInterval, $forecast];
}

/**
 * Generiert das HTML für den Energiefluss (SVG).
 * Zentralisiert für index.php und mobile.php.
 */
function renderEnergyFlow($layout = 'mobile', $extraClass = '', $extraAttributes = '') {
    global $wpEnabled, $wbEnabled, $hsEnabled;

    $cfgRes = loadE3dcConfig();
    $cfg = $cfgRes['config'] ?? [];
    $layout = $layout === 'desktop' ? 'desktop' : 'mobile';
    $uiFlow = getEnergyFlowUiConfig();
    $storedNodes = $uiFlow[$layout]['nodes'] ?? [];
    $colors = $uiFlow['colors'] ?? energyFlowDefaultColors();
    $aliases = $uiFlow['labels'] ?? [];
    $labelFor = function($key, $fallback) use ($aliases) {
        $alias = sanitizeEnergyFlowLabel($aliases[$key] ?? '');
        return htmlspecialchars($alias !== '' ? $alias : $fallback, ENT_QUOTES, 'UTF-8');
    };

    $showWpConfigured = isset($wpEnabled) ? $wpEnabled : isHeatpumpEnabledConfig($cfg);
    $showWp = $showWpConfigured || hasFreshMqttHeatpumpInbound($cfg);
    $showWb = hasWallbox1Config($cfg);
    $showWb2 = hasWallbox2Config($cfg);
    if (isset($wbEnabled) && !$wbEnabled) {
        $showWb = false;
        $showWb2 = false;
    }
    $showHs = isset($hsEnabled) ? $hsEnabled : isHeaterEnabledConfig($cfg);
    $showClimate = cfgBool($cfg['climate_enable'] ?? '0', false);
    $shellyOverride = strtolower(trim((string)($cfg['direct_marketing_aux_inverter_shelly_override'] ?? 'local')));
    $externalPvTopology = readExternalPvTopologyEvidence();
    $externalPvControlConfigured = cfgHasAddress($cfg['direct_marketing_aux_inverter_shelly_ip'] ?? '');
    $externalPvExplicitConfigured = $externalPvControlConfigured
        || in_array($shellyOverride, ['1', 'true', 'yes', 'on', 'ein', 'central', 'zentral'], true);
    $showExternalWr = $externalPvExplicitConfigured || !empty($externalPvTopology['topology_present']);
    $consumerKeys = ['home'];
    if ($showWp) $consumerKeys[] = 'heatpump';
    if ($showWb) $consumerKeys[] = 'wallbox';
    if ($showWb2) $consumerKeys[] = 'wallbox2';
    if ($showHs) $consumerKeys[] = 'heater';
    if ($showClimate) $consumerKeys[] = 'climate';
    $aggregateGeneration = $showExternalWr;
    $aggregateConsumption = count($consumerKeys) > 1;

    $wb1_type_raw = isset($cfg['wb_native_type']) && !empty($cfg['wb_native_type']) ? normalizeWallboxTypeConfig($cfg['wb_native_type']) : 'e3dc';
    $wb2_type_raw = isset($cfg['wb_native_type2']) && !empty($cfg['wb_native_type2']) ? normalizeWallboxTypeConfig($cfg['wb_native_type2']) : 'wb2';
    $wb_type_labels = [
        'openwb' => 'openWB',
        'openwb_pro' => 'openWB Pro',
        'goe' => 'go-e',
        'e3dc' => 'E3DC',
        'e3dc_easy' => 'E3DC',
        'e3dc_auto' => 'E3DC Auto',
        'e3dc_multi' => 'E3DC Multi',
        'e3dc_multi_connect' => 'E3DC Multi',
        'shelly' => 'Shelly',
        'tibber' => 'Tibber Pulse',
        'fronius' => 'Fronius'
    ];
    $short_type_labels = [
        'openwb' => 'openWB',
        'openwb_pro' => 'Pro',
        'goe' => 'go-e',
        'e3dc' => 'E3DC',
        'e3dc_easy' => 'E3DC',
        'e3dc_auto' => 'E3DC',
        'e3dc_multi' => 'Multi',
        'e3dc_multi_connect' => 'Multi',
        'shelly' => 'Shelly'
    ];
    $wb1_type = $wb_type_labels[strtolower($wb1_type_raw)] ?? ucfirst($wb1_type_raw);
    $wb2_type = $wb_type_labels[strtolower($wb2_type_raw)] ?? ucfirst($wb2_type_raw);
    $wb1_name_full = isset($cfg['wb1_name']) && !empty($cfg['wb1_name']) ? (string)$cfg['wb1_name'] : "$wb1_type (WB 1)";
    $wb2_name_full = isset($cfg['wb2_name']) && !empty($cfg['wb2_name']) ? (string)$cfg['wb2_name'] : "$wb2_type (WB 2)";
    $wb1_config_alias = sanitizeEnergyFlowLabel($cfg['wb1_name'] ?? '');
    $wb2_config_alias = sanitizeEnergyFlowLabel($cfg['wb2_name'] ?? '');
    $wb1_name = $labelFor('wallbox', ($wb1_config_alias !== '' && strlen((string)($cfg['wb1_name'] ?? '')) <= 32) ? $wb1_config_alias : 'Wallbox 1');
    $wb2_name = $labelFor('wallbox2', ($wb2_config_alias !== '' && strlen((string)($cfg['wb2_name'] ?? '')) <= 32) ? $wb2_config_alias : 'Wallbox 2');
    $wb1_title = htmlspecialchars($wb1_name_full);
    $wb2_title = htmlspecialchars($wb2_name_full);

    $pct = function($value) {
        return rtrim(rtrim(number_format((float)$value, 2, '.', ''), '0'), '.');
    };
    $pos = function($key, $defaultX, $defaultY) use ($storedNodes) {
        $node = (isset($storedNodes[$key]) && is_array($storedNodes[$key])) ? $storedNodes[$key] : [];
        return [
            'x' => normalizeEnergyFlowPercent($node['x'] ?? null, $defaultX),
            'y' => normalizeEnergyFlowPercent($node['y'] ?? null, $defaultY)
        ];
    };
    $rgba = function($hex, $alpha) {
        $hex = normalizeEnergyFlowColor($hex);
        $r = hexdec(substr($hex, 1, 2));
        $g = hexdec(substr($hex, 3, 2));
        $b = hexdec(substr($hex, 5, 2));
        return "rgba($r,$g,$b,$alpha)";
    };

    if ($layout === 'desktop') {
        $positions = [
            'pv' => $pos('pv', 20, 25),
            'external_pv' => $pos('external_pv', 20, 50),
            'grid' => $pos('grid', 20, 75),
            'battery' => $pos('battery', $showWb2 ? 38 : 50, $showWb2 ? 22 : 16),
            'home' => $pos('home', $showWb2 ? 62 : 72, $showWb2 ? 22 : 32),
            'heatpump' => $pos('heatpump', $showWb2 ? 68 : 72, 50),
            'wallbox' => $pos('wallbox', $showWb2 ? 38 : 72, $showWb2 ? 78 : 68),
            'wallbox2' => $pos('wallbox2', 62, 78),
            'climate' => $pos('climate', $showWb2 ? 82 : 84, $showWb2 ? 78 : 84),
            'generation' => $pos('generation', 35, 50),
            'consumption' => $pos('consumption', 65, 50),
        ];
        $hsDefaultX = $showWp ? ($showWb2 ? 62 : 55) : $positions['heatpump']['x'];
        $hsDefaultY = $showWp ? ($showWb2 ? 64 : 84) : $positions['heatpump']['y'];
        $positions['heater'] = $pos('heater', $hsDefaultX, $hsDefaultY);
        $hsSize = $showWp ? 'width: 90px; height: 90px;' : '';
        $hsIconSize = $showWp ? 'font-size: 1.6rem;' : '';
        $hsValSize = $showWp ? 'font-size: 1.0rem;' : '';
        $nodeGlow = 20;
    } else {
        $positions = [
            'pv' => $pos('pv', 20, 25),
            'external_pv' => $pos('external_pv', 20, 50),
            'grid' => $pos('grid', 20, 75),
            'battery' => $pos('battery', $showWb2 ? 38 : 50, $showWb2 ? 20 : 15),
            'home' => $pos('home', $showWb2 ? 62 : 76, $showWb2 ? 20 : 24),
            'heatpump' => $pos('heatpump', $showWb2 ? 70 : 76, $showWb2 ? 50 : 48),
            'wallbox' => $pos('wallbox', $showWb2 ? 38 : 76, $showWb2 ? 80 : 72),
            'wallbox2' => $pos('wallbox2', 62, 80),
            'heater' => $pos('heater', $showWb2 ? 62 : 55, $showWb2 ? 66 : 84),
            'climate' => $pos('climate', $showWb2 ? 82 : 76, $showWb2 ? 64 : 90),
            'generation' => $pos('generation', 34, 50),
            'consumption' => $pos('consumption', 66, 50),
        ];
        $hsSize = $showWp ? 'width: 60px; height: 60px; border-width: 2px;' : '';
        $hsIconSize = $showWp ? 'font-size: 1.0rem; margin-bottom: 0;' : '';
        $hsValSize = $showWp ? 'font-size: 0.7rem;' : '';
        $nodeGlow = 15;
    }
    if ($aggregateGeneration) {
        $positions['pv'] = $pos('pv', $layout === 'desktop' ? 15 : 10, 32);
        $positions['external_pv'] = $pos('external_pv', $layout === 'desktop' ? 15 : 10, 68);
        $positions['generation'] = $pos('generation', $layout === 'desktop' ? 35 : 30, 50);
    }
    if ($aggregateConsumption) {
        $count = count($consumerKeys);
        foreach ($consumerKeys as $idx => $key) {
            $rangeStart = $layout === 'desktop' ? 12 : 10;
            $rangeSize = $layout === 'desktop' ? 76 : 80;
            $defaultY = $count === 1 ? 50 : $rangeStart + ($idx * ($rangeSize / max(1, $count - 1)));
            $positions[$key] = $pos($key, $layout === 'desktop' ? 85 : 90, $defaultY);
        }
        $positions['consumption'] = $pos('consumption', $layout === 'desktop' ? 65 : 70, 50);
    }
    if ($aggregateGeneration || $aggregateConsumption) {
        $positions['battery'] = $pos('battery', 50, $layout === 'desktop' ? 16 : 14);
        $positions['grid'] = $pos('grid', 50, $layout === 'desktop' ? 84 : 86);
    }
    $positions['center'] = $pos('center', 50, 50);

    $nodeStyle = function($nodeKey, $colorKey, $extra = '') use ($positions, $colors, $pct, $rgba, $nodeGlow) {
        $p = $positions[$nodeKey];
        $color = htmlspecialchars($colors[$colorKey] ?? '#6c757d');
        $shadow = htmlspecialchars($rgba($color, 0.38));
        return 'top: ' . $pct($p['y']) . '%; left: ' . $pct($p['x']) . '%; --flow-node-color: ' . $color . '; border-color: ' . $color . '; color: ' . $color . '; box-shadow: 0 0 ' . $nodeGlow . 'px ' . $shadow . '; ' . $extra;
    };
    $nodeAttrs = function($nodeKey) use ($positions, $pct) {
        $p = $positions[$nodeKey] ?? ['x' => 50.0, 'y' => 50.0];
        return 'data-flow-x="' . $pct($p['x']) . '" data-flow-y="' . $pct($p['y']) . '"';
    };
    $linePair = function($nodeKey, $fromKey, $toKey, $colorKey, $lineId, $dotId) use ($positions, $colors, $pct) {
        $from = $positions[$fromKey];
        $to = $positions[$toKey];
        $color = htmlspecialchars($colors[$colorKey] ?? '#6c757d');
        return '<line x1="' . $pct($from['x']) . '%" y1="' . $pct($from['y']) . '%" x2="' . $pct($to['x']) . '%" y2="' . $pct($to['y']) . '%" class="flow-line" stroke="' . $color . '" id="' . $lineId . '" data-flow-line="' . $nodeKey . '" data-flow-from="' . $fromKey . '" data-flow-to="' . $toKey . '" data-flow-color-key="' . $colorKey . '" />
            <line x1="' . $pct($from['x']) . '%" y1="' . $pct($from['y']) . '%" x2="' . $pct($to['x']) . '%" y2="' . $pct($to['y']) . '%" class="flow-dots" id="' . $dotId . '" stroke="' . $color . '" data-flow-line="' . $nodeKey . '" data-flow-from="' . $fromKey . '" data-flow-to="' . $toKey . '" data-flow-color-key="' . $colorKey . '" />';
    };

    $colorOptions = [
        'pv' => 'Sonne',
        'external_pv' => 'Zusatz-WR',
        'grid' => 'Netz neutral',
        'grid_import' => 'Netzbezug',
        'grid_export' => 'Einspeisung',
        'battery' => 'Batterie entladen',
        'battery_charge' => 'Batterie laden',
        'home' => 'Haus',
    ];
    if ($showWb) $colorOptions['wallbox'] = 'Wallbox 1';
    if ($showWb2) $colorOptions['wallbox2'] = 'Wallbox 2';
    if ($showWp) $colorOptions['heatpump'] = 'WP';
    if ($showHs) $colorOptions['heater'] = 'Heizstab';
    if ($showClimate) $colorOptions['climate'] = 'Klima';
    if ($aggregateGeneration) $colorOptions['generation'] = 'Erzeugung';
    if ($aggregateConsumption) $colorOptions['consumption'] = 'Verbrauch';
    $colorOptions['center'] = 'E3DC-Control';
    $colorSelect = '';
    foreach ($colorOptions as $key => $label) {
        $colorSelect .= '<option value="' . htmlspecialchars($key) . '">' . htmlspecialchars($label) . '</option>';
    }

    $flowFlags = trim(($showWb2 ? ' flow-has-wb2' : '') . ($showWp ? ' flow-has-wp' : '') . ($showHs ? ' flow-has-hs' : '') . ($showClimate ? ' flow-has-climate' : '') . ($showExternalWr ? ' flow-has-external-wr' : '') . ($aggregateGeneration ? ' flow-has-generation-aggregate' : '') . ($aggregateConsumption ? ' flow-has-consumption-aggregate' : ''));
    $flowClasses = trim('flow-container ' . trim($extraClass) . ' ' . $flowFlags);

    return '
    <div id="flow-view" class="' . $flowClasses . '" data-flow-layout="' . htmlspecialchars($layout) . '" ' . $extraAttributes . '>
        <div class="flow-editor-toolbar" data-flow-toolbar>
            <span class="flow-save-status" data-flow-save-status role="status" aria-live="polite"></span>
            <button type="button" class="btn btn-sm btn-outline-secondary" data-flow-edit title="Layout bearbeiten" aria-label="Layout bearbeiten"><i class="fas fa-pen"></i></button>
            <div class="flow-editor-controls">
                <button type="button" class="btn btn-sm btn-outline-secondary" data-flow-auto title="Standardlayout" aria-label="Standardlayout"><i class="fas fa-wand-magic-sparkles"></i></button>
                <select class="form-select form-select-sm flow-color-select" data-flow-color-select title="Node-Farbe" aria-label="Node-Farbe">' . $colorSelect . '</select>
                <input type="color" class="form-control form-control-color flow-color-input" data-flow-color-input value="' . htmlspecialchars($colors['pv'] ?? '#ffc107') . '" title="Farbe" aria-label="Farbe">
                <input type="text" class="form-control form-control-sm flow-label-input" data-flow-label-input maxlength="32" autocomplete="off" placeholder="Anzeigename" title="Optionaler Anzeigename" aria-label="Optionaler Anzeigename">
                <button type="button" class="btn btn-sm btn-success" data-flow-save title="Speichern" aria-label="Speichern"><i class="fas fa-check"></i></button>
                <button type="button" class="btn btn-sm btn-outline-secondary" data-flow-cancel title="Abbrechen" aria-label="Abbrechen"><i class="fas fa-times"></i></button>
            </div>
        </div>
        <div class="flow-canvas" data-flow-canvas>
        <svg class="flow-svg">
            ' . ($aggregateGeneration
                ? $linePair('pv', 'pv', 'generation', 'pv', 'flow-line-pv', 'flow-dot-pv')
                    . $linePair('external_pv', 'external_pv', 'generation', 'external_pv', 'flow-line-external-pv', 'flow-dot-external-pv')
                    . $linePair('generation', 'generation', 'center', 'generation', 'flow-line-generation', 'flow-dot-generation')
                : $linePair('pv', 'pv', 'center', 'pv', 'flow-line-pv', 'flow-dot-pv')
                    . $linePair('external_pv', 'external_pv', 'center', 'external_pv', 'flow-line-external-pv', 'flow-dot-external-pv')) . '
            ' . $linePair('grid', 'grid', 'center', 'grid', 'flow-line-grid', 'flow-dot-grid') . '
            ' . $linePair('battery', 'battery', 'center', 'battery', 'flow-line-bat', 'flow-dot-bat') . '
            ' . ($aggregateConsumption ? $linePair('consumption', 'center', 'consumption', 'consumption', 'flow-line-consumption', 'flow-dot-consumption') : '') . '
            ' . $linePair('home', $aggregateConsumption ? 'consumption' : 'center', 'home', 'home', 'flow-line-home', 'flow-dot-home') . '
            ' . ($showWp ? $linePair('heatpump', $aggregateConsumption ? 'consumption' : 'center', 'heatpump', 'heatpump', 'flow-line-wp', 'flow-dot-wp') : '') . '
            ' . ($showWb ? $linePair('wallbox', $aggregateConsumption ? 'consumption' : 'center', 'wallbox', 'wallbox', 'flow-line-wb', 'flow-dot-wb') : '') . '
            ' . ($showWb2 ? $linePair('wallbox2', $aggregateConsumption ? 'consumption' : 'center', 'wallbox2', 'wallbox2', 'flow-line-wb2', 'flow-dot-wb2') : '') . '
            ' . ($showHs ? $linePair('heater', $aggregateConsumption ? 'consumption' : 'center', 'heater', 'heater', 'flow-line-hs', 'flow-dot-hs') : '') . '
            ' . ($showClimate ? $linePair('climate', $aggregateConsumption ? 'consumption' : 'center', 'climate', 'climate', 'flow-line-climate', 'flow-dot-climate') : '') . '
        </svg>

        <div class="flow-node-back" id="f-node-bat-back" data-flow-back="battery" ' . $nodeAttrs('battery') . ' style="top: '.$pct($positions['battery']['y']).'%; left: '.$pct($positions['battery']['x']).'%;"></div>

        ' . ($aggregateGeneration ? '<div class="flow-node node-aggregate node-generation" id="f-node-generation" data-flow-node="generation" data-flow-color-key="generation" ' . $nodeAttrs('generation') . ' style="' . $nodeStyle('generation', 'generation') . '"><i class="fas fa-bolt fa-icon"></i><div class="val" id="f-val-generation">0W</div><div class="label" data-flow-label-key="generation">' . $labelFor('generation', 'Erzeugung') . '</div></div>' : '') . '
        ' . ($aggregateConsumption ? '<div class="flow-node node-aggregate node-consumption" id="f-node-consumption" data-flow-node="consumption" data-flow-color-key="consumption" ' . $nodeAttrs('consumption') . ' style="' . $nodeStyle('consumption', 'consumption') . '"><i class="fas fa-gauge-high fa-icon"></i><div class="val" id="f-val-consumption">0W</div><div class="label" data-flow-label-key="consumption">' . $labelFor('consumption', 'Verbrauch') . '</div></div>' : '') . '

        <div class="flow-node node-pv" id="f-node-pv" data-flow-node="pv" data-flow-color-key="pv" ' . $nodeAttrs('pv') . ' style="' . $nodeStyle('pv', 'pv') . '"><i class="fas fa-sun fa-icon"></i><div class="val" id="f-val-pv">0W</div><div class="label flow-pv-split" id="f-val-pv-split" style="display:none;"></div><div class="label" data-flow-label-key="pv">' . $labelFor('pv', 'E3DC-PV') . '</div><div class="price-tag" id="f-val-pv-yield" style="display:none;"></div><span class="flow-zero-export-badge" id="f-pv-zero-export-badge" role="status" aria-live="polite" hidden><i class="fas fa-shield-alt" aria-hidden="true"></i><span id="f-pv-zero-export-label">LUOX 0 W</span></span></div>
        <div class="flow-node node-external-pv" id="f-node-external-pv" data-flow-node="external_pv" data-flow-color-key="external_pv" data-external-pv-configured="' . ($showExternalWr ? '1' : '0') . '" data-external-pv-topology="' . (!empty($externalPvTopology['topology_present']) ? '1' : '0') . '" data-external-pv-control-configured="' . ($externalPvControlConfigured ? '1' : '0') . '" ' . ($showExternalWr ? '' : 'hidden ') . $nodeAttrs('external_pv') . ' style="' . $nodeStyle('external_pv', 'external_pv') . '"><i class="fas fa-solar-panel fa-icon"></i><div class="val" id="f-val-external-pv">0W</div><div class="label" id="f-label-external-pv" data-flow-label-key="external_pv">' . $labelFor('external_pv', 'Zusatz-WR') . '</div><i class="fas fa-lock" id="f-external-pv-lock" style="display:none;"></i><button type="button" class="external-wr-lock-btn" id="f-external-pv-lock-btn" title="Zusatz-WR manuell sperren" aria-label="Zusatz-WR manuell sperren" aria-pressed="false" onclick="event.stopPropagation(); toggleDirectMarketingAuxInverterShellyLock(this);"' . ($externalPvControlConfigured ? '' : ' hidden disabled') . '><i class="fas fa-lock"></i></button></div>
        <div class="flow-node node-grid" id="f-node-grid" data-flow-node="grid" data-flow-color-key="grid" ' . $nodeAttrs('grid') . ' style="' . $nodeStyle('grid', 'grid') . '"><i class="fas fa-network-wired fa-icon"></i><div class="val" id="f-val-grid">0W</div><div class="label" data-flow-label-key="grid">' . $labelFor('grid', 'Netz') . '</div><div class="price-tag" id="f-val-price" style="display:none;"></div></div>
        <div class="flow-node node-bat" id="f-node-bat" data-flow-node="battery" data-flow-color-key="battery" ' . $nodeAttrs('battery') . ' style="' . $nodeStyle('battery', 'battery') . '"><i class="fas fa-battery-half fa-icon"></i><div class="val" id="f-val-bat">0W</div><div class="label" data-flow-label-key="battery">' . $labelFor('battery', 'Speicher') . '</div><div class="label flow-secondary-label" id="f-lbl-soc">0%</div></div>
        <div class="flow-node node-home" id="f-node-home" data-flow-node="home" data-flow-color-key="home" ' . $nodeAttrs('home') . ' style="' . $nodeStyle('home', 'home') . '"><i class="fas fa-home fa-icon"></i><div class="val" id="f-val-home">0W</div><div class="label" data-flow-label-key="home">' . $labelFor('home', 'Haus') . '</div></div>
        ' . ($showWp ? '<div class="flow-node node-wp" id="f-node-wp" data-flow-node="heatpump" data-flow-color-key="heatpump" ' . $nodeAttrs('heatpump') . ' style="' . $nodeStyle('heatpump', 'heatpump') . '"><i class="fas fa-fire fa-icon"></i><div class="val" id="f-val-wp">0W</div><div class="label" data-flow-label-key="heatpump">' . $labelFor('heatpump', 'Wärmepumpe') . '</div></div>' : '') . '
        ' . ($showWb ? '<div class="flow-node node-wb node-wb-1" id="f-node-wb" data-flow-node="wallbox" data-flow-color-key="wallbox" ' . $nodeAttrs('wallbox') . ' style="' . $nodeStyle('wallbox', 'wallbox') . '" title="'.$wb1_title.'"><i class="fas fa-charging-station fa-icon"></i><div class="val" id="f-val-wb">0W</div><div class="label" data-flow-label-key="wallbox">'.$wb1_name.'</div><i class="fas fa-lock" id="f-wb-lock" style="display:none; position:absolute; top:12%; right:22%; font-size:0.7rem; color:#ffc107;"></i><div class="price-tag" id="f-val-wb-session" style="display:none;"></div><div class="price-tag text-success border-success" id="f-val-car-soc" style="display:none; bottom: -42px; background: rgba(16, 185, 129, 0.1); cursor:pointer;" onclick="forceSocUpdate()" title="SoC vom Auto abrufen (Aufwecken)"></div></div>' : '') . '
        ' . ($showWb2 ? '<div class="flow-node node-wb node-wb-2" id="f-node-wb2" data-flow-node="wallbox2" data-flow-color-key="wallbox2" ' . $nodeAttrs('wallbox2') . ' style="' . $nodeStyle('wallbox2', 'wallbox2') . '" title="'.$wb2_title.'"><i class="fas fa-charging-station fa-icon"></i><div class="val" id="f-val-wb2">0W</div><div class="label" data-flow-label-key="wallbox2">'.$wb2_name.'</div><i class="fas fa-lock" id="f-wb2-lock" style="display:none; position:absolute; top:12%; right:22%; font-size:0.7rem; color:#ffc107;"></i><div class="price-tag" id="f-val-wb2-session" style="display:none;"></div><div class="price-tag text-success border-success" id="f-val-car-soc2" style="display:none; bottom: -42px; background: rgba(16, 185, 129, 0.1); cursor:pointer;" onclick="forceSocUpdate()" title="SoC vom Auto abrufen (Aufwecken)"></div></div>' : '') . '
        ' . ($showHs ? '<div class="flow-node node-hs" id="f-node-hs" data-flow-node="heater" data-flow-color-key="heater" ' . $nodeAttrs('heater') . ' style="' . $nodeStyle('heater', 'heater', $hsSize) . '"><i class="fas fa-fire-burner fa-icon" style="'.$hsIconSize.'"></i><div class="val" id="f-val-hs" style="'.$hsValSize.'">0W</div><div class="label" data-flow-label-key="heater" style="'.($showWp ? 'font-size: 0.5rem;' : '').'">' . $labelFor('heater', 'Heizstab') . '</div><div class="price-tag text-warning border-warning" id="f-val-hs-temp" style="display:none; bottom:-26px; background:rgba(253,126,20,0.12);"></div></div>' : '') . '
        ' . ($showClimate ? '<div class="flow-node node-climate" id="f-node-climate" data-flow-node="climate" data-flow-color-key="climate" ' . $nodeAttrs('climate') . ' style="' . $nodeStyle('climate', 'climate') . '"><i class="fas fa-snowflake fa-icon"></i><div class="val" id="f-val-climate">0W</div><div class="label" data-flow-label-key="climate">' . $labelFor('climate', 'Klima') . '</div></div>' : '') . '

        <div class="flow-node node-center" data-flow-node="center" data-flow-color-key="center" ' . $nodeAttrs('center') . ' style="cursor:pointer;" onclick="if (!this.closest(\'.flow-container\').classList.contains(\'flow-editing\')) { showDiagnoseLog(\'storage_manager\'); }" title="Storage Manager Protokoll anzeigen"><img src="app-icon-512.png" alt="Hub"></div>
        <div class="flow-hover-panel" data-flow-hover-panel aria-hidden="true"></div>
        </div>
    </div>';
}

/**
 * Berechnet die kumulierte Energieverteilung (kWh und %) für den aktuellen Tag
 * basierend auf den Einträgen der live_history.txt.
 */
function e3dcApplyExactEnergySplit(&$stats, $exactVal, $integratedTotal, $splitKeys, $fallbackKey = null) {
    $exactVal = (float)$exactVal;
    $integratedTotal = (float)$integratedTotal;
    if ($exactVal > 0) {
        if ($integratedTotal > 0.001) {
            $factor = $exactVal / $integratedTotal;
            if ($factor > 50.0 || $factor < 0.02) {
                foreach ($splitKeys as $k) {
                    if (isset($stats[$k]) && (strpos($k, 'cost_') === 0 || $k === 'cost_total' || strpos($k, 'save_') === 0)) {
                        $stats[$k] *= $factor;
                    }
                }
                if ($fallbackKey && isset($stats[$fallbackKey])) $stats[$fallbackKey] += ($exactVal - $integratedTotal);
                return $exactVal;
            }
            foreach ($splitKeys as $k) {
                if (isset($stats[$k])) $stats[$k] *= $factor;
            }
        } elseif ($exactVal > 0.01 && $fallbackKey) {
            $stats[$fallbackKey] = $exactVal;
        }
        return $exactVal;
    }
    return $integratedTotal;
}

function e3dcNormalizeEnergySourceBucket(&$stats, $totalKey, $splitKeys, $fallbackKey = null, $costBySplitKey = [], $totalCostKey = null) {
    $total = max(0.0, (float)($stats[$totalKey] ?? 0.0));
    $sum = 0.0;
    foreach ($splitKeys as $key) {
        $stats[$key] = max(0.0, (float)($stats[$key] ?? 0.0));
        $sum += $stats[$key];
    }

    if ($total <= 0.001) {
        foreach ($splitKeys as $key) {
            $stats[$key] = 0.0;
            if (isset($costBySplitKey[$key])) $stats[$costBySplitKey[$key]] = 0.0;
        }
        return;
    }

    if ($sum <= 0.001) {
        if ($fallbackKey && in_array($fallbackKey, $splitKeys, true)) {
            $stats[$fallbackKey] = $total;
            if ($totalCostKey && isset($costBySplitKey[$fallbackKey])) {
                $stats[$costBySplitKey[$fallbackKey]] = max(0.0, (float)($stats[$totalCostKey] ?? 0.0));
            }
        }
        return;
    }

    if ($sum > $total + 0.001 || $sum < $total - 0.001) {
        if ($sum > $total || !$fallbackKey || !in_array($fallbackKey, $splitKeys, true)) {
            $factor = $total / max(0.001, $sum);
            foreach ($splitKeys as $key) {
                $stats[$key] *= $factor;
                if (isset($costBySplitKey[$key])) $stats[$costBySplitKey[$key]] *= $factor;
            }
        } else {
            $delta = $total - $sum;
            $stats[$fallbackKey] += $delta;
            if ($totalCostKey && isset($costBySplitKey[$fallbackKey])) {
                $avgCostCt = max(0.0, (float)($stats[$totalCostKey] ?? 0.0)) / max(0.001, $total);
                $stats[$costBySplitKey[$fallbackKey]] = (float)($stats[$costBySplitKey[$fallbackKey]] ?? 0.0) + ($delta * $avgCostCt);
            }
        }
    }

    if ($totalCostKey && !empty($costBySplitKey)) {
        $targetCost = max(0.0, (float)($stats[$totalCostKey] ?? 0.0));
        $costSum = 0.0;
        foreach ($costBySplitKey as $costKey) {
            $stats[$costKey] = max(0.0, (float)($stats[$costKey] ?? 0.0));
            $costSum += $stats[$costKey];
        }
        if ($targetCost > 0.001 && $costSum > 0.001) {
            $factor = $targetCost / $costSum;
            foreach ($costBySplitKey as $costKey) {
                $stats[$costKey] *= $factor;
            }
        }
    }
}

function e3dcNormalizeGridExportSources(&$stats) {
    $totalGridOut = max(0.0, (float)($stats['total_grid_out'] ?? 0.0));
    $pvGrid = max(0.0, (float)($stats['pv_grid_kwh'] ?? 0.0));
    $batGrid = max(0.0, (float)($stats['bat_grid_kwh'] ?? 0.0));
    $sourceSum = $pvGrid + $batGrid;

    if ($totalGridOut <= 0.001) {
        $stats['pv_grid_kwh'] = 0.0;
        $stats['bat_grid_kwh'] = 0.0;
        $stats['cost_grid_out'] = 0.0;
        return;
    }

    if ($sourceSum > 0.001) {
        $stats['cost_grid_out'] = max(0.0, (float)($stats['cost_grid_out'] ?? 0.0)) * ($totalGridOut / $sourceSum);
    }

    $batGrid = min($batGrid, $totalGridOut);
    $stats['bat_grid_kwh'] = $batGrid;
    $stats['pv_grid_kwh'] = max(0.0, $totalGridOut - $batGrid);
}

function e3dcApplyExportBackedPvTotalFallback(&$stats) {
    $pv = max(0.0, (float)($stats['total_pv'] ?? 0));
    $gridOut = max(0.0, (float)($stats['total_grid_out'] ?? 0));
    $batOut = max(0.0, (float)($stats['total_bat_out'] ?? 0));

    if (!empty($stats['pv_exact_counter_present'])) return;
    if (($stats['pv_total_source'] ?? '') === 'integrated_total_with_external_ac') return;
    if ($pv <= 0.0001 || $gridOut <= 0.5) return;
    if ($gridOut <= ($pv + 0.5)) return;
    if ($gridOut <= max(1.0, $batOut * 1.5)) return;

    $stats['pv_total_raw_kwh'] = round($pv, 3);
    $stats['pv_total_export_fallback_kwh'] = round($gridOut, 3);
    $stats['total_pv'] = round($pv + $gridOut, 3);
}

function e3dcKeepIntegratedPvTotalForExternalAc($integratedPvKwh, $integratedDcKwh, $exactE3dcKwh) {
    $integratedPvKwh = max(0.0, (float)$integratedPvKwh);
    $integratedDcKwh = max(0.0, (float)$integratedDcKwh);
    $exactE3dcKwh = max(0.0, (float)$exactE3dcKwh);

    if ($integratedPvKwh <= 0.5 || $exactE3dcKwh <= 0.5 || $integratedDcKwh <= 0.5) return false;
    if ($integratedPvKwh <= $exactE3dcKwh + max(2.0, $exactE3dcKwh * 0.08)) return false;

    // Wenn DC-Integration und E3DC-Zähler eng beieinander liegen, die
    // Live-PV-Leistung aber deutlich höher ist, enthält sie sehr wahrscheinlich
    // externen AC-PV-Anteil. Dann darf der Gesamt-PV-Ertrag nicht auf den
    // E3DC-Zähler reduziert werden.
    return abs($integratedDcKwh - $exactE3dcKwh) <= max(5.0, $exactE3dcKwh * 0.20);
}

function e3dcWallboxExactCounterNeedsIntegralGuard($source) {
    $source = strtolower(trim((string)$source));
    if ($source === '') return false;
    foreach (['native_session_integrated', 'live_wallbox_energy', 'wallbox_native_detail'] as $needle) {
        if (strpos($source, $needle) !== false) return true;
    }
    return false;
}

function e3dcSanitizeWallboxExactCounter($exactKwh, $integratedKwh, $source, &$sanity = null) {
    $sanity = null;
    if (!is_numeric($exactKwh)) return null;
    $exactKwh = (float)$exactKwh;
    if ($exactKwh < 0 || $exactKwh >= 2000) return null;

    $integratedKwh = max(0.0, is_numeric($integratedKwh) ? (float)$integratedKwh : 0.0);
    if (!e3dcWallboxExactCounterNeedsIntegralGuard($source)) {
        if ($integratedKwh > 0.5) {
            $maxExtreme = max($integratedKwh * 3.0, $integratedKwh + 25.0);
            if ($exactKwh > $maxExtreme) {
                $sanity = [
                    'action' => 'extreme_integral_cap',
                    'raw_kwh' => $exactKwh,
                    'integral_kwh' => $integratedKwh,
                    'source' => (string)$source,
                ];
                return $integratedKwh;
            }
        }
        return $exactKwh;
    }

    if ($integratedKwh <= 0.05) {
        if ($exactKwh > 1.0) {
            $sanity = [
                'action' => 'reject_without_integral',
                'raw_kwh' => $exactKwh,
                'integral_kwh' => $integratedKwh,
                'source' => (string)$source,
            ];
            return 0.0;
        }
        return $exactKwh;
    }

    $maxPlausible = max($integratedKwh * 1.35, $integratedKwh + 2.0);
    if ($exactKwh > $maxPlausible) {
        $sanity = [
            'action' => 'integral_cap',
            'raw_kwh' => $exactKwh,
            'integral_kwh' => $integratedKwh,
            'source' => (string)$source,
        ];
        return $integratedKwh;
    }

    return $exactKwh;
}

function e3dcWallboxExactSourceFromHistoryRow($row, $exactKey) {
    if (!is_array($row)) return '';
    if ($exactKey === 'e_wb2') {
        foreach (['e_wb2_source', 'wb2_daily_source', 'wb2_source'] as $key) {
            $source = trim((string)($row[$key] ?? ''));
            if ($source !== '') return $source;
        }
        return '';
    }
    if ($exactKey === 'e_wb') {
        foreach (['e_wb_source', 'wb_daily_source', 'wb_source'] as $key) {
            $source = trim((string)($row[$key] ?? ''));
            if ($source !== '') return $source;
        }
    }
    return '';
}

function applyExactWallboxDailyCounter(&$dailyStats, $wallboxNo, $exactKwh, $source = 'wallbox_daily_counter') {
    if (!is_array($dailyStats) || !isset($dailyStats['stats']) || !is_array($dailyStats['stats'])) return;
    if (!is_numeric($exactKwh)) return;
    $exactKwh = (float)$exactKwh;
    if ($exactKwh < 0 || $exactKwh >= 2000) return;

    $key = ((int)$wallboxNo === 2) ? 'wb2' : 'wb';
    $stats =& $dailyStats['stats'];
    $pvKey = "pv_{$key}_kwh";
    $gridKey = "grid_{$key}_kwh";
    $batKey = "bat_{$key}_kwh";
    $totalKey = "total_{$key}_kwh";

    $current = (float)($stats[$totalKey] ?? (
        (float)($stats[$pvKey] ?? 0) + (float)($stats[$gridKey] ?? 0) + (float)($stats[$batKey] ?? 0)
    ));
    $sanity = null;
    $effectiveKwh = e3dcSanitizeWallboxExactCounter($exactKwh, $current, $source, $sanity);
    if ($effectiveKwh === null) return;
    if ($effectiveKwh <= 0.001 && $current <= 0.001) return;

    if ($current > 0.001) {
        $factor = $effectiveKwh / $current;
        foreach ([$pvKey, $gridKey, $batKey] as $splitKey) {
            $stats[$splitKey] = round((float)($stats[$splitKey] ?? 0) * $factor, 2);
        }
        if (isset($dailyStats['costs']) && is_array($dailyStats['costs'])) {
            if (isset($dailyStats['costs'][$key])) $dailyStats['costs'][$key] = round((float)$dailyStats['costs'][$key] * $factor, 2);
            $saveKey = "save_{$key}";
            if (isset($dailyStats['costs'][$saveKey])) $dailyStats['costs'][$saveKey] = round((float)$dailyStats['costs'][$saveKey] * $factor, 2);
        }
    } else {
        $stats[$pvKey] = round($effectiveKwh, 2);
        $stats[$gridKey] = 0;
        $stats[$batKey] = 0;
    }

    $stats[$totalKey] = round($effectiveKwh, 2);

    $totalPv = max(0.001, (float)($stats['total_pv_kwh'] ?? 0));
    $totalGrid = max(0.001, (float)($stats['total_grid_in_kwh'] ?? 0));
    $totalBat = max(0.001, (float)($stats['total_bat_out_kwh'] ?? 0));
    $stats["pv_{$key}_pct"] = round(((float)($stats[$pvKey] ?? 0) / $totalPv) * 100);
    $stats["grid_{$key}_pct"] = round(((float)($stats[$gridKey] ?? 0) / $totalGrid) * 100);
    $stats["bat_{$key}_pct"] = round(((float)($stats[$batKey] ?? 0) / $totalBat) * 100);

	    $totalCons =
	        (float)($stats['total_home_kwh'] ?? 0) +
	        (float)($stats['total_wb_kwh'] ?? 0) +
	        (float)($stats['total_wb2_kwh'] ?? 0) +
	        (float)($stats['total_wp_kwh'] ?? 0) +
	        (float)($stats['total_climate_kwh'] ?? 0);
    if ($totalCons > 0.001) {
        $gridIn = (float)($stats['total_grid_in_kwh'] ?? 0);
        $dailyStats['autarky_day'] = round(max(0, min(100, (($totalCons - $gridIn) / $totalCons) * 100)), 1);
    }

    if (!isset($dailyStats['sources']) || !is_array($dailyStats['sources'])) {
        $dailyStats['sources'] = [];
    }
    $dailyStats['sources']["{$key}_daily_kwh"] = round($effectiveKwh, 3);
    $dailyStats['sources']["{$key}_daily_source"] = (string)$source;
    if (is_array($sanity)) {
        $dailyStats['sources']["{$key}_daily_raw_kwh"] = round((float)$sanity['raw_kwh'], 3);
        $dailyStats['sources']["{$key}_daily_integral_kwh"] = round((float)$sanity['integral_kwh'], 3);
        $dailyStats['sources']["{$key}_daily_sanity"] = (string)$sanity['action'];
    }
}

function e3dcHistoryUsesStiebelWpCounter($historyLines, $activeDate) {
    foreach ($historyLines as $ln) {
        $d = json_decode($ln, true);
        if (!$d || !isset($d['ts']) || strpos($d['ts'], $activeDate) !== 0) continue;
        $wpType = isset($d['wp_type']) ? (string)$d['wp_type'] : '';
        $source = strtolower((string)($d['e_wp_source'] ?? ''));
        if ($wpType === '4' || strpos($source, 'stiebel') === 0) return true;
    }
    return false;
}

function e3dcDailyExactBaselines($historyLines, $activeDate, $keys, $options = []) {
    $baselines = array_fill_keys($keys, 0.0);
    $midnight = strtotime($activeDate . ' 00:00:00');
    if ($midnight === false) return $baselines;
    $skipBaseline = [];
    if (!empty($options['stiebel_wp_counter']) || e3dcHistoryUsesStiebelWpCounter($historyLines, $activeDate)) {
        $skipBaseline['e_wp'] = true;
    }

    $first = [];
    $earlyMin = [];
    foreach ($historyLines as $ln) {
        $d = json_decode($ln, true);
        if (!$d || !isset($d['ts']) || strpos($d['ts'], $activeDate) !== 0) continue;
        $ts = strtotime($d['ts']);
        if ($ts === false) continue;
        $age = $ts - $midnight;
        if ($age < 0 || $age > 3600) continue;

        foreach ($keys as $k) {
            if (!empty($skipBaseline[$k])) continue;
            if (!isset($d[$k]) || !is_numeric($d[$k])) continue;
            $val = (float)$d[$k];
            if ($val < 0 || $val >= 2000) continue;
            if (!isset($first[$k])) {
                if ($val <= 0) continue;
                $first[$k] = ['ts' => $ts, 'val' => $val];
                $earlyMin[$k] = $val;
            } else {
                $earlyMin[$k] = min($earlyMin[$k], $val);
            }
        }
    }

    foreach ($first as $k => $info) {
        $firstAge = (float)$info['ts'] - (float)$midnight;
        $firstVal = (float)$info['val'];
        $minVal = (float)($earlyMin[$k] ?? $firstVal);
        // E3DC-DB-History kann kurz nach Mitternacht den letzten Bucket von
        // gestern als Tagesanfang liefern. Wenn dieser Wert nicht zeitnah auf
        // null zurückfaellt, ist er die Baseline und kein heutiger Verbrauch.
        if ($firstAge <= 300.0 && $firstVal > 0.05 && ($minVal + 0.05) >= $firstVal) {
            $baselines[$k] = $firstVal;
        }
    }

    return $baselines;
}

function e3dcHistoryWallboxHomeRelation($row, $consumerKey) {
    if (!is_array($row)) return false;
    $isWb2 = ((string)$consumerKey === 'wb2');
    $relationKey = $isWb2 ? 'wb2_home_relation' : 'wb_home_relation';
    $relation = strtolower(trim((string)($row[$relationKey] ?? '')));
    if ($relation !== '') return $relation;

    $typeKey = $isWb2 ? 'wb2_native_type' : 'wb_native_type';
    $type = normalizeWallboxTypeConfig($row[$typeKey] ?? '');
    $sourceKeys = $isWb2
        ? ['wb2_source', 'e_wb2_source', 'wb2_daily_source']
        : ['wb_source', 'e_wb_source', 'wb_daily_source'];
    $isE3dcMulti = in_array($type, ['e3dc_multi', 'e3dc_multi_connect'], true);
    foreach ($sourceKeys as $sourceKey) {
        $source = strtolower(trim((string)($row[$sourceKey] ?? '')));
        if (strpos($source, 'e3dc_multi') !== false) $isE3dcMulti = true;
    }
    if (!$isE3dcMulti || !isset($row['home_raw']) || !is_numeric($row['home_raw'])) return '';

    $powerKey = $isWb2 ? 'wb2' : 'wb';
    $wb = max(0.0, e3dcHistoryNumber($row, $powerKey, 0.0));
    if ($wb <= 50.0) return '';
    $pv = e3dcHistoryNumber($row, 'pv', 0.0);
    $grid = e3dcHistoryNumber($row, 'grid', 0.0);
    $bat = e3dcHistoryNumber($row, 'bat', 0.0);
    $homeRaw = max(0.0, e3dcHistoryNumber($row, 'home_raw', 0.0));
    $totalLoad = $pv + $grid - $bat;
    if ($totalLoad < -500.0 || $totalLoad > 60000.0) return '';

    $tolerance = max(250.0, min(1200.0, $wb * 0.18));
    $netGap = $totalLoad - $homeRaw;
    if (abs($netGap - $wb) <= $tolerance) return 'home_excludes_wb';
    if (abs($totalLoad - $homeRaw) <= $tolerance && ($homeRaw + $tolerance) >= $wb) return 'home_includes_wb';
    if (abs($totalLoad - $homeRaw) <= $tolerance && ($homeRaw + $tolerance) < $wb) return 'stale_balance_reject';

    return '';
}

function e3dcHistoryRowMarksExternalConsumer($row, $consumerKey) {
    if (!is_array($row)) return false;
    $isWb2 = ((string)$consumerKey === 'wb2');
    $relation = e3dcHistoryWallboxHomeRelation($row, $consumerKey);
    if (in_array($relation, ['home_includes_wb', 'home_includes_wallbox'], true)) return true;
    if (in_array($relation, ['home_excludes_wb', 'home_excludes_wallbox', 'stale_balance_reject'], true)) return false;

    $flagKey = $isWb2 ? 'is_external_wb2' : 'is_external_wb';
    if (!empty($row[$flagKey])) return true;

    $typeKey = $isWb2 ? 'wb2_native_type' : 'wb_native_type';
    $type = normalizeWallboxTypeConfig($row[$typeKey] ?? '');
    if ($type !== '' && $type !== 'none' && strpos($type, 'e3dc') !== 0) return true;

    $sourceKeys = $isWb2
        ? ['wb2_source', 'e_wb2_source', 'wb2_daily_source']
        : ['wb_source', 'e_wb_source', 'wb_daily_source'];
    foreach ($sourceKeys as $sourceKey) {
        $source = strtolower(trim((string)($row[$sourceKey] ?? '')));
        if ($source === '') continue;
        foreach (['openwb', 'mqtt', 'external', 'evcc', 'go-e', 'goe', 'shelly', 'e3dc_multi'] as $needle) {
            if (strpos($source, $needle) !== false) return true;
        }
    }
    return false;
}

function e3dcHistoryNumber($row, $key, $default = 0.0) {
    if (!is_array($row) || !isset($row[$key]) || !is_numeric($row[$key])) return $default;
    return (float)$row[$key];
}

function e3dcCleanHistoryHomePower($row) {
    if (!is_array($row)) return 0.0;
    $home = isset($row['home']) && is_numeric($row['home'])
        ? (float)$row['home']
        : e3dcHistoryNumber($row, 'home_raw', 0.0);
    $home = max(0.0, $home);

    if (!isset($row['home_raw']) || !is_numeric($row['home_raw'])) {
        return $home;
    }
    $homeRaw = max(0.0, (float)$row['home_raw']);
    foreach (['wb', 'wb2'] as $consumerKey) {
        $relation = e3dcHistoryWallboxHomeRelation($row, $consumerKey);
        if (!in_array($relation, ['home_excludes_wb', 'home_excludes_wallbox', 'stale_balance_reject'], true)) continue;
        $wbPower = max(0.0, e3dcHistoryNumber($row, $consumerKey, 0.0));
        $threshold = ($relation === 'stale_balance_reject') ? 50.0 : max(250.0, min(1200.0, $wbPower * 0.18));
        if ($homeRaw > 50.0 && $home < ($homeRaw - $threshold)) {
            $home = $homeRaw;
        }
    }

    $externalLoad = 0.0;
    if (e3dcHistoryRowMarksExternalConsumer($row, 'wb')) {
        $externalLoad += max(0.0, e3dcHistoryNumber($row, 'wb', 0.0));
    }
    if (e3dcHistoryRowMarksExternalConsumer($row, 'wb2')) {
        $externalLoad += max(0.0, e3dcHistoryNumber($row, 'wb2', 0.0));
    }
    $externalLoad += max(
        max(0.0, e3dcHistoryNumber($row, 'wp', 0.0)),
        max(0.0, e3dcHistoryNumber($row, 'hs', 0.0))
    );
    $externalLoad += max(0.0, e3dcHistoryNumber($row, 'climate', 0.0));
    if ($externalLoad <= 50.0) return $home;

    $cleanFromRaw = max(0.0, $homeRaw - $externalLoad);
    $threshold = max(250.0, min(1500.0, $externalLoad * 0.15));
    if ($home > ($cleanFromRaw + $threshold)) {
        return $cleanFromRaw;
    }
    return $home;
}

function e3dcCleanExactHomeEnergy($exactHome, $wbEnergy = 0.0, $wb2Energy = 0.0, $wpEnergy = 0.0, $climateEnergy = 0.0) {
    $home = max(0.0, is_numeric($exactHome) ? (float)$exactHome : 0.0);
    if ($home <= 0.0) return $home;

    $consumerTotal =
        max(0.0, is_numeric($wbEnergy) ? (float)$wbEnergy : 0.0)
        + max(0.0, is_numeric($wb2Energy) ? (float)$wb2Energy : 0.0)
        + max(0.0, is_numeric($wpEnergy) ? (float)$wpEnergy : 0.0)
        + max(0.0, is_numeric($climateEnergy) ? (float)$climateEnergy : 0.0);
    if ($consumerTotal <= 0.05) return $home;
    if (($home + 0.05) < $consumerTotal) return $home;

    return max(0.0, $home - $consumerTotal);
}

function e3dcFitPvSourceDestinations(&$stats, $source, $sourceTotalKey, &$availableByDestination) {
    $destinations = ['home', 'bat', 'wb', 'wb2', 'wp', 'climate', 'grid'];
    $availableTotal = array_sum($availableByDestination);
    $target = min(
        max(0.0, (float)($stats[$sourceTotalKey] ?? 0.0)),
        max(0.0, $availableTotal)
    );
    $values = [];
    $seeded = [];
    foreach ($destinations as $destination) {
        $key = "pv_{$source}_{$destination}_kwh";
        $capacity = max(0.0, (float)($availableByDestination[$destination] ?? 0.0));
        $seed = min($capacity, max(0.0, (float)($stats[$key] ?? 0.0)));
        $values[$destination] = $seed;
        $seeded[$destination] = $seed > 0.000001;
    }

    $allocated = array_sum($values);
    if ($allocated > $target + 0.000001) {
        $factor = $target / max(0.000001, $allocated);
        foreach ($destinations as $destination) {
            $values[$destination] *= $factor;
        }
    } elseif ($allocated < $target - 0.000001) {
        $remaining = $target - $allocated;
        foreach ([true, false] as $seededOnly) {
            if ($remaining <= 0.000001) break;
            $weights = [];
            $weightTotal = 0.0;
            foreach ($destinations as $destination) {
                $capacity = max(
                    0.0,
                    (float)($availableByDestination[$destination] ?? 0.0) - $values[$destination]
                );
                if ($capacity <= 0.000001 || ($seededOnly && !$seeded[$destination])) continue;
                $weight = $seededOnly ? max(0.000001, $values[$destination]) : $capacity;
                $weights[$destination] = [$weight, $capacity];
                $weightTotal += $weight;
            }
            if ($weightTotal <= 0.000001) continue;
            $roundBudget = $remaining;
            foreach ($weights as $destination => [$weight, $capacity]) {
                $addition = min($capacity, $roundBudget * ($weight / $weightTotal));
                $values[$destination] += $addition;
            }
            $remaining = max(0.0, $target - array_sum($values));
        }
    }

    foreach ($destinations as $destination) {
        $key = "pv_{$source}_{$destination}_kwh";
        $value = min(
            max(0.0, (float)($availableByDestination[$destination] ?? 0.0)),
            max(0.0, (float)($values[$destination] ?? 0.0))
        );
        $stats[$key] = $value;
        $availableByDestination[$destination] = max(
            0.0,
            (float)($availableByDestination[$destination] ?? 0.0) - $value
        );
    }
}

function e3dcFinalizePvSourceDestinations(&$stats) {
    $available = [];
    foreach (['home', 'bat', 'wb', 'wb2', 'wp', 'climate', 'grid'] as $destination) {
        $available[$destination] = max(0.0, (float)($stats["pv_{$destination}_kwh"] ?? 0.0));
    }
    e3dcFitPvSourceDestinations($stats, 'external', 'pv_external_kwh', $available);
    e3dcFitPvSourceDestinations($stats, 'e3dc', 'pv_e3dc_kwh', $available);
}

function calculateDailyEnergyStats($historyLines, $options = []) {
    $lastTs = null;
    $lastRow = null;
    $activeDate = null;

    // Wir lesen das Datum aus dem aktuellsten Eintrag (ganz unten),
    // damit bei der 48h-Historie der heutige Tag gewertet wird.
    for ($i = count($historyLines) - 1; $i >= 0; $i--) {
        $d = @json_decode($historyLines[$i], true);
        if ($d && isset($d['ts'])) {
            $activeDate = substr($d['ts'], 0, 10);
            break;
        }
    }
    if (!$activeDate) $activeDate = date('Y-m-d');

    $stats = [
        'pv_home_kwh' => 0, 'pv_bat_kwh' => 0, 'pv_wb_kwh' => 0, 'pv_wb2_kwh' => 0, 'pv_wp_kwh' => 0, 'pv_climate_kwh' => 0, 'pv_grid_kwh' => 0,
        'pv_e3dc_kwh' => 0, 'pv_external_kwh' => 0, 'pv_source_rest_kwh' => 0,
        'grid_home_kwh' => 0, 'grid_bat_kwh' => 0, 'grid_wb_kwh' => 0, 'grid_wb2_kwh' => 0, 'grid_wp_kwh' => 0, 'grid_climate_kwh' => 0,
        'bat_home_kwh' => 0, 'bat_wb_kwh' => 0, 'bat_wb2_kwh' => 0, 'bat_wp_kwh' => 0, 'bat_climate_kwh' => 0, 'bat_grid_kwh' => 0,
        'total_consumption' => 0, 'total_pv' => 0, 'total_pv_dc' => 0, 'total_grid_in' => 0, 'total_grid_out' => 0,
        'total_bat_out' => 0, 'total_bat_in' => 0,
        'cost_total' => 0, 'cost_grid_in' => 0, 'cost_grid_out' => 0,
        'cost_home' => 0, 'cost_bat' => 0, 'cost_wb' => 0, 'cost_wb2' => 0, 'cost_wp' => 0, 'cost_climate' => 0,
        'save_total' => 0, 'save_home' => 0, 'save_wb' => 0, 'save_wb2' => 0, 'save_wp' => 0, 'save_climate' => 0,
        'sum_price' => 0, 'count_price' => 0
    ];
    foreach (['external', 'e3dc'] as $pvSource) {
        foreach (['home', 'bat', 'wb', 'wb2', 'wp', 'climate', 'grid'] as $destination) {
            $stats["pv_{$pvSource}_{$destination}_kwh"] = 0.0;
        }
    }

    foreach ($historyLines as $ln) {
        $d = json_decode($ln, true);
        if (!$d || !isset($d['ts']) || strpos($d['ts'], $activeDate) !== 0) continue;

        $ts = strtotime($d['ts']);
        if ($lastTs !== null) {
            $dt = $ts - $lastTs; // in Sekunden
            if ($dt > 0 && $dt < 3600) {
                $dtHours = $dt / 3600;

                $pNow = $d['price_ct'] ?? null;
                $pPrev = $lastRow['price_ct'] ?? null;
                if (is_numeric($pNow) && is_numeric($pPrev)) {
                    $price_ct = ((float)$pNow + (float)$pPrev) / 2;
                } elseif (is_numeric($pNow)) {
                    $price_ct = (float)$pNow;
                } elseif (is_numeric($pPrev)) {
                    $price_ct = (float)$pPrev;
                } else {
                    $price_ct = 0.0;
                }
                if (is_numeric($pNow) || is_numeric($pPrev)) {
                    $stats['sum_price'] += $price_ct;
                    $stats['count_price']++;
                }

                // Mittelwerte für das Intervall
                $pv = max(0, (($d['pv'] ?? 0) + ($lastRow['pv'] ?? 0)) / 2);
                $pvExternalNow = isset($d['pv_external_w']) && is_numeric($d['pv_external_w']) ? (float)$d['pv_external_w'] : null;
                $pvExternalPrev = isset($lastRow['pv_external_w']) && is_numeric($lastRow['pv_external_w']) ? (float)$lastRow['pv_external_w'] : null;
                if ($pvExternalNow !== null || $pvExternalPrev !== null) {
                    $pvExternal = max(0, (($pvExternalNow ?? $pvExternalPrev ?? 0) + ($pvExternalPrev ?? $pvExternalNow ?? 0)) / 2);
                    $pvExternal = min($pv, $pvExternal);
                } else {
                    $pvExternal = 0.0;
                }
                $pvE3dc = max(0, $pv - $pvExternal);
                $dcNow = max(0, (float)($d['dc0_w'] ?? 0) + (float)($d['dc1_w'] ?? 0));
                $dcPrev = max(0, (float)($lastRow['dc0_w'] ?? 0) + (float)($lastRow['dc1_w'] ?? 0));
                $pvDc = max(0, ($dcNow + $dcPrev) / 2);
                $grid = (($d['grid'] ?? 0) + ($lastRow['grid'] ?? 0)) / 2;
                $bat = (($d['bat'] ?? 0) + ($lastRow['bat'] ?? 0)) / 2;
                $home = max(0, (e3dcCleanHistoryHomePower($d) + e3dcCleanHistoryHomePower($lastRow)) / 2);
                $wb = max(0, (($d['wb'] ?? 0) + ($lastRow['wb'] ?? 0)) / 2);
                $wb2 = max(0, (($d['wb2'] ?? 0) + ($lastRow['wb2'] ?? 0)) / 2);
                $wp = max(0, (($d['wp'] ?? 0) + ($lastRow['wp'] ?? 0)) / 2);
                $climate = max(0, (($d['climate'] ?? 0) + ($lastRow['climate'] ?? 0)) / 2);

                $grid_in = max(0, $grid); $grid_out = max(0, -$grid);
                $bat_in = max(0, $bat);
                $bat_out = max(0, -$bat);
                $loads = $home + $wb + $wb2 + $wp + $climate;

                $stats['total_consumption'] += ($loads * $dtHours) / 1000;
                $stats['total_pv'] += ($pv * $dtHours) / 1000;
                $stats['pv_e3dc_kwh'] += ($pvE3dc * $dtHours) / 1000;
                $stats['pv_external_kwh'] += ($pvExternal * $dtHours) / 1000;
                $stats['total_pv_dc'] += ($pvDc * $dtHours) / 1000;
                $stats['total_grid_in'] += ($grid_in * $dtHours) / 1000;
                $stats['total_grid_out'] += ($grid_out * $dtHours) / 1000;
                $stats['total_bat_out'] += ($bat_out * $dtHours) / 1000;
                $stats['total_bat_in'] += ($bat_in * $dtHours) / 1000;

                // Verteilung Sonnenenergie (PV)
                $pv_to_loads = min($pv, $loads);
                $pv_excess = max(0, $pv - $pv_to_loads);
                $pv_to_bat = min($pv_excess, $bat_in);
                $pv_to_grid = max(0, $pv - $pv_to_loads - $pv_to_bat);

                $externalShare = $pv > 0.001 ? min(1.0, $pvExternal / $pv) : 0.0;
                $externalChargeLocked = !empty($d['pv_external_charge_locked'])
                    || !empty($lastRow['pv_external_charge_locked']);
                if ($externalChargeLocked) {
                    $externalToBat = 0.0;
                    $nonBatteryPv = $pv_to_loads + $pv_to_grid;
                    $externalToLoads = $nonBatteryPv > 0.001
                        ? min($pv_to_loads, $pvExternal * ($pv_to_loads / $nonBatteryPv))
                        : 0.0;
                    $externalToGrid = min($pv_to_grid, max(0.0, $pvExternal - $externalToLoads));
                    $externalRemainder = max(0.0, $pvExternal - $externalToLoads - $externalToGrid);
                    if ($externalRemainder > 0.001) {
                        $loadRoom = max(0.0, $pv_to_loads - $externalToLoads);
                        $addToLoads = min($loadRoom, $externalRemainder);
                        $externalToLoads += $addToLoads;
                        $externalRemainder -= $addToLoads;
                    }
                    if ($externalRemainder > 0.001) {
                        $gridRoom = max(0.0, $pv_to_grid - $externalToGrid);
                        $externalToGrid += min($gridRoom, $externalRemainder);
                    }
                } else {
                    $externalToLoads = $pv_to_loads * $externalShare;
                    $externalToBat = $pv_to_bat * $externalShare;
                    $externalToGrid = $pv_to_grid * $externalShare;
                }
                $e3dcToLoads = max(0.0, $pv_to_loads - $externalToLoads);
                $e3dcToBat = max(0.0, $pv_to_bat - $externalToBat);
                $e3dcToGrid = max(0.0, $pv_to_grid - $externalToGrid);

                $stats['pv_bat_kwh'] += ($pv_to_bat * $dtHours) / 1000;
                $stats['pv_grid_kwh'] += ($pv_to_grid * $dtHours) / 1000;
                $stats['pv_external_bat_kwh'] += ($externalToBat * $dtHours) / 1000;
                $stats['pv_external_grid_kwh'] += ($externalToGrid * $dtHours) / 1000;
                $stats['pv_e3dc_bat_kwh'] += ($e3dcToBat * $dtHours) / 1000;
                $stats['pv_e3dc_grid_kwh'] += ($e3dcToGrid * $dtHours) / 1000;
                $remainingLoadsAfterPv = max(0, $loads - $pv_to_loads);
                $bat_to_loads = min($bat_out, $remainingLoadsAfterPv);
                $bat_excess = max(0, $bat_out - $bat_to_loads);
                $bat_to_grid = min($bat_excess, $grid_out);

                $stats['bat_grid_kwh'] += ($bat_to_grid * $dtHours) / 1000;
                $stats['cost_grid_out'] += (($pv_to_grid + $bat_to_grid) * $dtHours / 1000) * $price_ct;

                if ($loads > 0) {
                    $stats['pv_home_kwh'] += ($pv_to_loads * ($home / $loads) * $dtHours) / 1000;
                    $stats['pv_wb_kwh'] += ($pv_to_loads * ($wb / $loads) * $dtHours) / 1000;
                    $stats['pv_wb2_kwh'] += ($pv_to_loads * ($wb2 / $loads) * $dtHours) / 1000;
                    $stats['pv_wp_kwh'] += ($pv_to_loads * ($wp / $loads) * $dtHours) / 1000;
                    $stats['pv_climate_kwh'] += ($pv_to_loads * ($climate / $loads) * $dtHours) / 1000;
                    foreach ([
                        'home' => $home,
                        'wb' => $wb,
                        'wb2' => $wb2,
                        'wp' => $wp,
                        'climate' => $climate,
                    ] as $destination => $loadW) {
                        $loadShare = $loadW / $loads;
                        $stats["pv_external_{$destination}_kwh"] += ($externalToLoads * $loadShare * $dtHours) / 1000;
                        $stats["pv_e3dc_{$destination}_kwh"] += ($e3dcToLoads * $loadShare * $dtHours) / 1000;
                    }
                }

                // Verteilung Netzbezug
                $grid_to_bat = max(0, $bat_in - $pv_excess);
                $grid_to_loads = max(0, $grid_in - $grid_to_bat);

                $stats['grid_bat_kwh'] += ($grid_to_bat * $dtHours) / 1000;
                $stats['cost_total'] += ($grid_in * $dtHours / 1000) * $price_ct;
                $stats['cost_bat'] += ($grid_to_bat * $dtHours / 1000) * $price_ct;

                if ($loads > 0) {
                    $stats['grid_home_kwh'] += ($grid_to_loads * ($home / $loads) * $dtHours) / 1000;
                    $stats['grid_wb_kwh'] += ($grid_to_loads * ($wb / $loads) * $dtHours) / 1000;
                    $stats['grid_wb2_kwh'] += ($grid_to_loads * ($wb2 / $loads) * $dtHours) / 1000;
                    $stats['grid_wp_kwh'] += ($grid_to_loads * ($wp / $loads) * $dtHours) / 1000;
                    $stats['grid_climate_kwh'] += ($grid_to_loads * ($climate / $loads) * $dtHours) / 1000;

                    $stats['cost_home'] += ($grid_to_loads * ($home / $loads) * $dtHours / 1000) * $price_ct;
                    $stats['cost_wb'] += ($grid_to_loads * ($wb / $loads) * $dtHours / 1000) * $price_ct;
                    $stats['cost_wb2'] += ($grid_to_loads * ($wb2 / $loads) * $dtHours / 1000) * $price_ct;
                    $stats['cost_wp'] += ($grid_to_loads * ($wp / $loads) * $dtHours / 1000) * $price_ct;
                    $stats['cost_climate'] += ($grid_to_loads * ($climate / $loads) * $dtHours / 1000) * $price_ct;
                }

                // Ersparnis-Berechnung (Was wurde durch PV/Bat gespart?)
                if ($loads > 0) {
                    $pv_share = $pv_to_loads;
                    $bat_share = $bat_to_loads;
                    $self_share = $pv_share + $bat_share;

                    $stats['save_home'] += ($self_share * ($home / $loads) * $dtHours / 1000) * $price_ct;
                    $stats['save_wb'] += ($self_share * ($wb / $loads) * $dtHours / 1000) * $price_ct;
                    $stats['save_wb2'] += ($self_share * ($wb2 / $loads) * $dtHours / 1000) * $price_ct;
                    $stats['save_wp'] += ($self_share * ($wp / $loads) * $dtHours / 1000) * $price_ct;
                    $stats['save_climate'] += ($self_share * ($climate / $loads) * $dtHours / 1000) * $price_ct;
                    $stats['save_total'] += ($self_share * $dtHours / 1000) * $price_ct;
                }

                // Verteilung Batterie-Entladung
                if ($bat_to_loads > 0 && $loads > 0) {
                    $stats['bat_home_kwh'] += ($bat_to_loads * ($home / $loads) * $dtHours) / 1000;
                    $stats['bat_wb_kwh'] += ($bat_to_loads * ($wb / $loads) * $dtHours) / 1000;
                    $stats['bat_wb2_kwh'] += ($bat_to_loads * ($wb2 / $loads) * $dtHours) / 1000;
                    $stats['bat_wp_kwh'] += ($bat_to_loads * ($wp / $loads) * $dtHours) / 1000;
                    $stats['bat_climate_kwh'] += ($bat_to_loads * ($climate / $loads) * $dtHours) / 1000;
                }
            }
        }
        $lastTs = $ts; $lastRow = $d;
    }

    // --- SICHERHEITS-KORREKTUR FÜR HANGOVER-GLITCHES ---
    // Die e_* Werte sind echte Systemzähler für den jeweiligen Tag und summieren auf.
    // Anstatt Differenzen zu summieren (was bei Glitches zu extremen "Ratchet"-Fehlern führt),
    // übernehmen wir einfach den allerletzten gültigen Wert des Tages.
    $finalExact = ['e_pv' => 0, 'e_grid_in' => 0, 'e_grid_out' => 0, 'e_bat_out' => 0, 'e_bat_in' => 0, 'e_home' => 0, 'e_wb' => 0, 'e_wb2' => 0, 'e_wp' => 0, 'e_climate' => 0];
    $finalExactSource = array_fill_keys(array_keys($finalExact), '');
    $exactBaselines = e3dcDailyExactBaselines($historyLines, $activeDate, array_keys($finalExact), $options);

    foreach ($historyLines as $ln) {
        $d = json_decode($ln, true);
        if (!$d || !isset($d['ts']) || strpos($d['ts'], $activeDate) !== 0) continue;

        foreach (array_keys($finalExact) as $k) {
            if (isset($d[$k])) {
                $val = (float)$d[$k];

                // Schutz gegen massive RSCP-Glitches (z.B. 0xFFFFFFFF Fehler = 4294967 kWh).
                // 0.0 ist nach Mitternacht ein gültiger Reset und darf alte
                // Hangover-Werte von gestern aktiv überschreiben.
                if ($val >= 0 && $val < 2000) {
                    $baseline = (float)($exactBaselines[$k] ?? 0.0);
                    if ($baseline > 0.0) {
                        $val = max(0.0, $val - $baseline);
                    }
                    // Wir überschreiben den gemerkten Wert, WENN:
                    // 1. Er noch 0 ist (Start)
                    // 2. Er wächst (normaler Tagesverlauf)
                    // 3. Er massiv (> 5 kWh) fällt => Löst das Problem des "Hangover-Glitches":
                    //    Wenn E3DC kurz nach 0 Uhr noch den Wert von gestern funkt (z.B. 52 kWh)
                    //    und im folgenden Tick auf reale 0.001 kWh fällt.
                    if ($finalExact[$k] == 0 || $val > $finalExact[$k] || ($finalExact[$k] - $val > 5.0)) {
                        $finalExact[$k] = $val;
                        $finalExactSource[$k] = e3dcWallboxExactSourceFromHistoryRow($d, $k);
                    }
                }
            }
        }
    }

    // --- EXAKTE ZÄHLER (C++) ANWENDEN & VERTEILUNGEN SKALIEREN ---
    $integratedPvTotal = (float)($stats['total_pv'] ?? 0.0);
    $integratedDcPvTotal = (float)($stats['total_pv_dc'] ?? 0.0);
    if ($finalExact['e_pv'] > 0.001) {
        $stats['pv_exact_counter_present'] = true;
        if (e3dcKeepIntegratedPvTotalForExternalAc($integratedPvTotal, $integratedDcPvTotal, $finalExact['e_pv'])) {
            $stats['pv_total_source'] = 'integrated_total_with_external_ac';
            $stats['pv_total_integrated_kwh'] = round($integratedPvTotal, 3);
            $stats['pv_e3dc_exact_kwh'] = round($finalExact['e_pv'], 3);
            $stats['pv_dc_integrated_kwh'] = round($integratedDcPvTotal, 3);
            $stats['pv_external_integrated_kwh'] = round(max(0.0, $integratedPvTotal - max($integratedDcPvTotal, $finalExact['e_pv'])), 3);
            $inferredExternalPv = max(0.0, $integratedPvTotal - max($integratedDcPvTotal, $finalExact['e_pv']));
            if ($inferredExternalPv > (float)($stats['pv_external_kwh'] ?? 0.0) + max(0.5, $integratedPvTotal * 0.05)) {
                $stats['pv_external_kwh'] = $inferredExternalPv;
                $stats['pv_e3dc_kwh'] = max(0.0, $integratedPvTotal - $inferredExternalPv);
            }
        } else {
            $stats['pv_total_source'] = 'exact_e3dc_counter';
            $stats['pv_total_integrated_kwh'] = round($integratedPvTotal, 3);
            $stats['pv_e3dc_exact_kwh'] = round($finalExact['e_pv'], 3);
            $stats['total_pv'] = e3dcApplyExactEnergySplit($stats, $finalExact['e_pv'], $stats['total_pv'], ['pv_home_kwh', 'pv_bat_kwh', 'pv_wb_kwh', 'pv_wb2_kwh', 'pv_wp_kwh', 'pv_climate_kwh'], 'pv_home_kwh');
        }
    } else {
        $stats['pv_total_source'] = 'power_integral';
    }
    $stats['total_grid_in'] = e3dcApplyExactEnergySplit($stats, $finalExact['e_grid_in'], $stats['total_grid_in'], ['grid_home_kwh', 'grid_bat_kwh', 'grid_wb_kwh', 'grid_wb2_kwh', 'grid_wp_kwh', 'grid_climate_kwh', 'cost_total', 'cost_home', 'cost_bat', 'cost_wb', 'cost_wb2', 'cost_wp', 'cost_climate'], 'grid_home_kwh');
    $stats['total_grid_out'] = e3dcApplyExactEnergySplit($stats, $finalExact['e_grid_out'], $stats['total_grid_out'], ['cost_grid_out'], null);
    $stats['total_bat_out'] = e3dcApplyExactEnergySplit($stats, $finalExact['e_bat_out'], $stats['total_bat_out'], ['bat_home_kwh', 'bat_wb_kwh', 'bat_wb2_kwh', 'bat_wp_kwh', 'bat_climate_kwh', 'bat_grid_kwh'], 'bat_home_kwh');
    $stats['total_bat_in'] = e3dcApplyExactEnergySplit($stats, $finalExact['e_bat_in'], $stats['total_bat_in'], [], null);
    e3dcNormalizeGridExportSources($stats);
    e3dcApplyExportBackedPvTotalFallback($stats);
    $pvSourceSum = max(0.0, (float)($stats['pv_e3dc_kwh'] ?? 0.0)) + max(0.0, (float)($stats['pv_external_kwh'] ?? 0.0));
    if ($pvSourceSum <= 0.001 && $stats['total_pv'] > 0.001) {
        $stats['pv_e3dc_kwh'] = $stats['total_pv'];
        $stats['pv_external_kwh'] = 0.0;
        $stats['pv_source_rest_kwh'] = 0.0;
    } elseif ($pvSourceSum > $stats['total_pv'] + 0.001) {
        $factor = $stats['total_pv'] / max(0.001, $pvSourceSum);
        $stats['pv_e3dc_kwh'] *= $factor;
        $stats['pv_external_kwh'] *= $factor;
        $stats['pv_source_rest_kwh'] = 0.0;
    } else {
        $stats['pv_source_rest_kwh'] = max(0.0, $stats['total_pv'] - $pvSourceSum);
    }
    $wallboxExactSanity = [];

    // Exact Values für Wallbox und WP anwenden, falls vorhanden (native Zähler aus C++)
    if ($finalExact['e_wb'] > 0) {
        $wbSum = $stats['pv_wb_kwh'] + $stats['grid_wb_kwh'] + $stats['bat_wb_kwh'];
        $sanity = null;
        $finalExact['e_wb'] = e3dcSanitizeWallboxExactCounter($finalExact['e_wb'], $wbSum, $finalExactSource['e_wb'] ?? '', $sanity);
        if (is_array($sanity)) $wallboxExactSanity['wb'] = $sanity;
        if ($finalExact['e_wb'] > 0) {
            e3dcApplyExactEnergySplit($stats, $finalExact['e_wb'], $wbSum, ['pv_wb_kwh', 'grid_wb_kwh', 'bat_wb_kwh', 'cost_wb', 'save_wb'], 'pv_wb_kwh');
        }
    }
    if ($finalExact['e_wb2'] > 0) {
        $wb2Sum = $stats['pv_wb2_kwh'] + $stats['grid_wb2_kwh'] + $stats['bat_wb2_kwh'];
        $sanity = null;
        $finalExact['e_wb2'] = e3dcSanitizeWallboxExactCounter($finalExact['e_wb2'], $wb2Sum, $finalExactSource['e_wb2'] ?? '', $sanity);
        if (is_array($sanity)) $wallboxExactSanity['wb2'] = $sanity;
        if ($finalExact['e_wb2'] > 0) {
            e3dcApplyExactEnergySplit($stats, $finalExact['e_wb2'], $wb2Sum, ['pv_wb2_kwh', 'grid_wb2_kwh', 'bat_wb2_kwh', 'cost_wb2', 'save_wb2'], 'pv_wb2_kwh');
        }
    }
    if ($finalExact['e_wp'] > 0) {
        $wpSum = $stats['pv_wp_kwh'] + $stats['grid_wp_kwh'] + $stats['bat_wp_kwh'];
        e3dcApplyExactEnergySplit($stats, $finalExact['e_wp'], $wpSum, ['pv_wp_kwh', 'grid_wp_kwh', 'bat_wp_kwh', 'cost_wp', 'save_wp'], null);
    }
    if ($finalExact['e_climate'] > 0) {
        $climateSum = $stats['pv_climate_kwh'] + $stats['grid_climate_kwh'] + $stats['bat_climate_kwh'];
        e3dcApplyExactEnergySplit($stats, $finalExact['e_climate'], $climateSum, ['pv_climate_kwh', 'grid_climate_kwh', 'bat_climate_kwh', 'cost_climate', 'save_climate'], null);
    }
    // RSCP Home_Energy_kWh ist ein Brutto-Hauszähler. Für Langzeit muss "Haus"
    // ohne separat ausgewiesene Wallboxen und Wärmepumpe gespeichert werden.
    $cleanExactHome = $finalExact['e_home'];
    if ($cleanExactHome > 0) {
        $wbEnergyForHome = $finalExact['e_wb'] > 0
            ? $finalExact['e_wb']
            : ($stats['pv_wb_kwh'] + $stats['grid_wb_kwh'] + $stats['bat_wb_kwh']);
        $wb2EnergyForHome = $finalExact['e_wb2'] > 0
            ? $finalExact['e_wb2']
            : ($stats['pv_wb2_kwh'] + $stats['grid_wb2_kwh'] + $stats['bat_wb2_kwh']);
        $wpEnergyForHome = $finalExact['e_wp'] > 0
            ? $finalExact['e_wp']
            : ($stats['pv_wp_kwh'] + $stats['grid_wp_kwh'] + $stats['bat_wp_kwh']);
        $climateEnergyForHome = $finalExact['e_climate'] > 0
            ? $finalExact['e_climate']
            : ($stats['pv_climate_kwh'] + $stats['grid_climate_kwh'] + $stats['bat_climate_kwh']);
        $cleanExactHome = e3dcCleanExactHomeEnergy(
            $cleanExactHome,
            $wbEnergyForHome,
            $wb2EnergyForHome,
            $wpEnergyForHome,
            $climateEnergyForHome
        );
    }
    $stats['total_home'] = e3dcApplyExactEnergySplit(
        $stats,
        $cleanExactHome,
        ($stats['pv_home_kwh'] + $stats['grid_home_kwh'] + $stats['bat_home_kwh']),
        ['pv_home_kwh', 'grid_home_kwh', 'bat_home_kwh', 'cost_home', 'save_home'],
        'pv_home_kwh'
    );

    // Exakte Verbraucherzähler können einzelne Zeilen nachträglich skalieren.
    // Danach muss jede Quellenkarte wieder auf ihre echte Quellenenergie passen:
    // "Netzbezug 1.46 kWh" darf nicht 2.69 kWh in Batterie plus weitere Lasten zeigen.
    e3dcNormalizeEnergySourceBucket(
        $stats,
        'total_pv',
        ['pv_home_kwh', 'pv_bat_kwh', 'pv_wb_kwh', 'pv_wb2_kwh', 'pv_wp_kwh', 'pv_climate_kwh', 'pv_grid_kwh'],
        'pv_home_kwh'
    );
    e3dcNormalizeEnergySourceBucket(
        $stats,
        'total_grid_in',
        ['grid_home_kwh', 'grid_bat_kwh', 'grid_wb_kwh', 'grid_wb2_kwh', 'grid_wp_kwh', 'grid_climate_kwh'],
        'grid_home_kwh',
        [
            'grid_home_kwh' => 'cost_home',
            'grid_bat_kwh' => 'cost_bat',
            'grid_wb_kwh' => 'cost_wb',
            'grid_wb2_kwh' => 'cost_wb2',
            'grid_wp_kwh' => 'cost_wp',
            'grid_climate_kwh' => 'cost_climate',
        ],
        'cost_total'
    );
    e3dcNormalizeEnergySourceBucket(
        $stats,
        'total_bat_out',
        ['bat_home_kwh', 'bat_wb_kwh', 'bat_wb2_kwh', 'bat_wp_kwh', 'bat_climate_kwh', 'bat_grid_kwh'],
        'bat_home_kwh'
    );

    e3dcNormalizeEnergySourceBucket(
        $stats,
        'total_bat_out',
        ['bat_home_kwh', 'bat_wb_kwh', 'bat_wb2_kwh', 'bat_wp_kwh', 'bat_climate_kwh', 'bat_grid_kwh'],
        ((float)($stats['bat_grid_kwh'] ?? 0.0) > 0.05) ? 'bat_grid_kwh' : 'bat_home_kwh'
    );
    e3dcNormalizeGridExportSources($stats);

    // Alle Quellen-Normalisierungen können einzelne Verbraucheranteile wieder
    // hochskalieren. Der bereinigte exakte Hauszähler bleibt als letzte harte
    // Tagesgröße für "Haus ohne Wallbox/Wärmepumpe/Klima" führend.
    if ($cleanExactHome > 0) {
        e3dcApplyExactEnergySplit(
            $stats,
            $cleanExactHome,
            ($stats['pv_home_kwh'] + $stats['grid_home_kwh'] + $stats['bat_home_kwh']),
            ['pv_home_kwh', 'grid_home_kwh', 'bat_home_kwh', 'cost_home', 'save_home'],
            'pv_home_kwh'
        );
    }

    // AC-seitig ist die Herkunft einzelner Elektronen nicht messbar. Die
    // Quellenziele bleiben deshalb eine bilanzielle Intervallzuordnung und
    // werden hier auf die finalen Tageszaehler und Zielbuckets begrenzt.
    e3dcFinalizePvSourceDestinations($stats);

    $stats['total_consumption'] = ($stats['pv_home_kwh'] + $stats['grid_home_kwh'] + $stats['bat_home_kwh']) + ($stats['pv_wb_kwh'] + $stats['grid_wb_kwh'] + $stats['bat_wb_kwh']) + ($stats['pv_wb2_kwh'] + $stats['grid_wb2_kwh'] + $stats['bat_wb2_kwh']) + ($stats['pv_wp_kwh'] + $stats['grid_wp_kwh'] + $stats['bat_wp_kwh']) + ($stats['pv_climate_kwh'] + $stats['grid_climate_kwh'] + $stats['bat_climate_kwh']);

    // Autarkie & Eigenverbrauch
    $totalPv = max(0.001, $stats['total_pv']);
    $totalGridIn = $stats['total_grid_in'];
    $totalGridOut = $stats['total_grid_out'];
    $totalCons = max(0.001, $stats['total_consumption']);
    $totalBatOut = max(0.001, $stats['total_bat_out']);

    $res = [
        'pv_today_kwh' => round($stats['total_pv'], 2),
        'autarky_day' => round(max(0, min(100, (($totalCons - $totalGridIn) / $totalCons) * 100)), 1),
        'selfcon_day' => round(max(0, min(100, (($totalPv - $totalGridOut) / $totalPv) * 100)), 1),
        'stats' => []
    ];
    $pvSourceInfo = [];
    foreach ([
        'pv_total_source',
        'pv_total_integrated_kwh',
        'pv_e3dc_exact_kwh',
        'pv_dc_integrated_kwh',
        'pv_external_integrated_kwh',
        'pv_total_raw_kwh',
        'pv_total_export_fallback_kwh',
    ] as $sourceKey) {
        if (array_key_exists($sourceKey, $stats)) {
            $value = $stats[$sourceKey];
            $pvSourceInfo[$sourceKey] = is_numeric($value) ? round((float)$value, 3) : $value;
        }
    }
    if ($pvSourceInfo) {
        $res['sources'] = $pvSourceInfo;
    }

    $res['stats']['total_pv_kwh'] = round($totalPv, 2);
    $res['stats']['total_grid_in_kwh'] = round($totalGridIn, 2);
    $res['stats']['total_grid_out_kwh'] = round($totalGridOut, 2);
    $res['stats']['total_bat_out_kwh'] = round($totalBatOut, 2);
    $res['stats']['total_bat_in_kwh'] = round($stats['total_bat_in'], 2);
    $res['stats']['pv_e3dc_kwh'] = round($stats['pv_e3dc_kwh'], 2);
    $res['stats']['pv_external_kwh'] = round($stats['pv_external_kwh'], 2);
    $res['stats']['pv_source_rest_kwh'] = round($stats['pv_source_rest_kwh'], 2);
    $res['stats']['pv_e3dc_pct'] = round(($stats['pv_e3dc_kwh'] / $totalPv) * 100);
    $res['stats']['pv_external_pct'] = round(($stats['pv_external_kwh'] / $totalPv) * 100);
    $res['stats']['pv_source_rest_pct'] = round(($stats['pv_source_rest_kwh'] / $totalPv) * 100);
    foreach (['external' => 'pv_external_kwh', 'e3dc' => 'pv_e3dc_kwh'] as $pvSource => $sourceTotalKey) {
        $sourceTotal = max(0.001, (float)($stats[$sourceTotalKey] ?? 0.0));
        foreach (['home', 'bat', 'wb', 'wb2', 'wp', 'climate', 'grid'] as $destination) {
            $key = "pv_{$pvSource}_{$destination}_kwh";
            $value = max(0.0, (float)($stats[$key] ?? 0.0));
            $res['stats'][$key] = round($value, 2);
            $res['stats']["pv_{$pvSource}_{$destination}_pct"] = round(($value / $sourceTotal) * 100);
        }
    }

    $res['stats']['total_home_kwh'] = round(($stats['pv_home_kwh'] + $stats['grid_home_kwh'] + $stats['bat_home_kwh']), 2);
    $res['stats']['total_wb_kwh'] = round(($stats['pv_wb_kwh'] + $stats['grid_wb_kwh'] + $stats['bat_wb_kwh']), 2);
    $res['stats']['total_wb2_kwh'] = round(($stats['pv_wb2_kwh'] + $stats['grid_wb2_kwh'] + $stats['bat_wb2_kwh']), 2);
    $res['stats']['total_wp_kwh'] = round(($stats['pv_wp_kwh'] + $stats['grid_wp_kwh'] + $stats['bat_wp_kwh']), 2);
    $res['stats']['total_climate_kwh'] = round(($stats['pv_climate_kwh'] + $stats['grid_climate_kwh'] + $stats['bat_climate_kwh']), 2);

    // %-Werte berechnen
    foreach (['home', 'bat', 'wb', 'wb2', 'wp', 'climate', 'grid'] as $key) {
        $val = $stats["pv_{$key}_kwh"];
        $res['stats']["pv_{$key}_kwh"] = round($val, 2);
        $res['stats']["pv_{$key}_pct"] = round(($val / $totalPv) * 100);
    }
    $totalGridInSafe = max(0.001, $totalGridIn);
    foreach (['home', 'bat', 'wb', 'wb2', 'wp', 'climate'] as $key) {
        $val = $stats["grid_{$key}_kwh"];
        $res['stats']["grid_{$key}_kwh"] = round($val, 2);
        $res['stats']["grid_{$key}_pct"] = round(($val / $totalGridInSafe) * 100);
    }
    foreach (['home', 'wb', 'wb2', 'wp', 'climate', 'grid'] as $key) {
        $val = $stats["bat_{$key}_kwh"];
        $res['stats']["bat_{$key}_kwh"] = round($val, 2);
        $res['stats']["bat_{$key}_pct"] = round(($val / $totalBatOut) * 100);
    }

    $res['costs'] = [
        'total' => round($stats['cost_total'] / 100, 2),
        'home' => round($stats['cost_home'] / 100, 2),
        'bat' => round($stats['cost_bat'] / 100, 2),
        'wb' => round($stats['cost_wb'] / 100, 2),
        'wb2' => round($stats['cost_wb2'] / 100, 2),
        'wp' => round($stats['cost_wp'] / 100, 2),
        'climate' => round($stats['cost_climate'] / 100, 2),
        'save_total' => round($stats['save_total'] / 100, 2),
        'save_home' => round($stats['save_home'] / 100, 2),
        'save_wb' => round($stats['save_wb'] / 100, 2),
        'save_wb2' => round($stats['save_wb2'] / 100, 2),
        'save_wp' => round($stats['save_wp'] / 100, 2),
        'save_climate' => round($stats['save_climate'] / 100, 2),
        'avg_price' => $totalGridIn > 0.1
            ? min(round($stats['cost_total'] / $totalGridIn, 1), 100)
            : ($stats['count_price'] > 0 ? round($stats['sum_price'] / $stats['count_price'], 1) : 0)
    ];
    if ($finalExact['e_wb'] > 0 || $finalExact['e_wb2'] > 0) {
        if (!isset($res['sources']) || !is_array($res['sources'])) $res['sources'] = [];
        if ($finalExact['e_wb'] > 0) {
            $res['sources']['wb_daily_kwh'] = round($finalExact['e_wb'], 3);
            if (!empty($finalExactSource['e_wb'])) $res['sources']['wb_daily_source'] = (string)$finalExactSource['e_wb'];
        }
        if ($finalExact['e_wb2'] > 0) {
            $res['sources']['wb2_daily_kwh'] = round($finalExact['e_wb2'], 3);
            if (!empty($finalExactSource['e_wb2'])) $res['sources']['wb2_daily_source'] = (string)$finalExactSource['e_wb2'];
        }
    }
    foreach ($wallboxExactSanity as $key => $sanity) {
        if (!isset($res['sources']) || !is_array($res['sources'])) $res['sources'] = [];
        $res['sources']["{$key}_daily_raw_kwh"] = round((float)$sanity['raw_kwh'], 3);
        $res['sources']["{$key}_daily_integral_kwh"] = round((float)$sanity['integral_kwh'], 3);
        $res['sources']["{$key}_daily_sanity"] = (string)$sanity['action'];
    }
    foreach ($exactBaselines as $k => $baseline) {
        if ((float)$baseline > 0.0) {
            if (!isset($res['sources']) || !is_array($res['sources'])) $res['sources'] = [];
            $res['sources']["{$k}_midnight_baseline_kwh"] = round((float)$baseline, 3);
        }
    }
    return $res;
}

/**
 * Generiert das HTML für das Grid-Health Dashboard (Phasengenaue Schieflast / Belastung).
 */
function renderGridHealthModal($dialogClass = 'modal-md modal-dialog-scrollable') {
    return renderE3dcModalThemeStyles() . '
    <div class="modal fade" id="gridHealthModal" tabindex="-1" aria-hidden="true">
        <div class="modal-dialog ' . $dialogClass . '">
            <div class="modal-content bg-body-secondary text-body border-secondary" style="border-radius: 12px; border: 1px solid rgba(255,255,255,0.1);">
                <div class="modal-header border-secondary d-flex justify-content-between w-100 align-items-center">
                    <h5 class="modal-title"><i class="fas fa-bolt me-2 text-warning"></i>Grid Health</h5>
                    <button type="button" class="btn-close e3dc-modal-close" data-bs-dismiss="modal" aria-label="Close"></button>
                </div>
                <div class="modal-body p-3">

                    <!-- Netzfrequenz-Anzeige -->
                    <div class=mb-4>
                        <h6 class=text-muted text-uppercase small fw-bold mb-2 d-flex justify-content-between align-items-center>
                            Netzfrequenz
                            <span id=gh-freq-badge class=badge bg-success style=font-size:0.65em;>50.00 Hz</span>
                        </h6>
                        <div class=position-relative style=height:22px; background:linear-gradient(to right,#dc3545 0%,#ffc107 12%,#198754 25%,#198754 75%,#ffc107 88%,#dc3545 100%); border-radius:10px; overflow:hidden;>
                            <div id=gh-freq-marker style=position:absolute;top:0;bottom:0;width:3px;background:#fff;border-radius:2px;left:50%;transform:translateX(-50%);transition:left 0.6s ease;box-shadow:0 0 6px rgba(0,0,0,0.6);></div>
                        </div>
                        <div class="position-relative mt-1" style="height:14px;font-size:0.62rem;color:var(--bs-secondary-color);">
                            <span style="position:absolute;left:0.0%;transform:translateX(-50%);">49.7</span><span style="position:absolute;left:16.67%;transform:translateX(-50%);">49.8</span><span style="position:absolute;left:33.33%;transform:translateX(-50%);">49.9</span><span style="position:absolute;left:50.0%;transform:translateX(-50%);font-weight:bold;color:var(--bs-body-color);">50</span><span style="position:absolute;left:66.67%;transform:translateX(-50%);">50.1</span><span style="position:absolute;left:83.33%;transform:translateX(-50%);">50.2</span><span style="position:absolute;left:100.0%;transform:translateX(-50%);">50.3</span>
                        </div>
                        <div class=mt-2 small id=gh-freq-text style=color:var(--bs-secondary-color);>Netzfrequenz wird geladen...</div>
                    </div>
                    <hr class=border-secondary mb-4>
                                        <!-- Ampel / Warnhinweis für Schieflast -->
                    <div id="gridHealthAlert" class="alert alert-success d-flex align-items-center py-2 px-3 mb-4 border-0" style="border-radius: 10px;">
                        <i id="gridHealthIcon" class="fas fa-check-circle fs-3 me-3"></i>
                        <div>
                            <div class="fw-bold mb-0" id="gridHealthTitle" style="font-size: 1.1rem;">Schieflast OK</div>
                            <div class="small" id="gridHealthText">Netzbelastung ist symmetrisch.</div>
                        </div>
                    </div>

                    <h6 class="text-muted text-uppercase small fw-bold mb-3 d-flex justify-content-between align-items-center">
                        Hausnetz (Sensor)
                        <span id="gh-max-scale" class="badge bg-secondary" style="font-size: 0.65em;">Max: --A</span>
                    </h6>

                    <div class="mb-3">
                        <div class="d-flex justify-content-between small mb-1">
                            <span class="text-body">L1 (Phase 1)</span>
                            <span id="gh-l1-w" class="fw-bold text-body">0 W</span>
                        </div>
                        <div class="progress" style="height: 12px; background-color: rgba(255,255,255,0.1); border-radius: 8px;">
                            <div id="gh-l1-bar" class="progress-bar bg-info" role="progressbar" style="width: 0%; transition: width 0.5s ease;"></div>
                        </div>
                        <div class="text-end mt-1"><span id="gh-l1-a" class="text-muted" style="font-size: 0.75rem;">0.0 A</span></div>
                    </div>

                    <div class="mb-3">
                        <div class="d-flex justify-content-between small mb-1">
                            <span class="text-body">L2 (Phase 2)</span>
                            <span id="gh-l2-w" class="fw-bold text-body">0 W</span>
                        </div>
                        <div class="progress" style="height: 12px; background-color: rgba(255,255,255,0.1); border-radius: 8px;">
                            <div id="gh-l2-bar" class="progress-bar bg-info" role="progressbar" style="width: 0%; transition: width 0.5s ease;"></div>
                        </div>
                        <div class="text-end mt-1"><span id="gh-l2-a" class="text-muted" style="font-size: 0.75rem;">0.0 A</span></div>
                    </div>

                    <div class="mb-3">
                        <div class="d-flex justify-content-between small mb-1">
                            <span class="text-body">L3 (Phase 3)</span>
                            <span id="gh-l3-w" class="fw-bold text-body">0 W</span>
                        </div>
                        <div class="progress" style="height: 12px; background-color: rgba(255,255,255,0.1); border-radius: 8px;">
                            <div id="gh-l3-bar" class="progress-bar bg-info" role="progressbar" style="width: 0%; transition: width 0.5s ease;"></div>
                        </div>
                        <div class="text-end mt-1"><span id="gh-l3-a" class="text-muted" style="font-size: 0.75rem;">0.0 A</span></div>
                    </div>

                    <!-- Optionale Wallbox-Phasen (blenden sich ein wenn vorhanden) -->
                    <div id="gh-wb-container" style="display: none;">
                        <hr class="border-secondary my-4">
                        <h6 class="text-muted text-uppercase small fw-bold mb-3 d-flex justify-content-between align-items-center">
                            Wallbox (Phasenaufteilung)
                        </h6>
                        <div class="row g-2 text-center small text-body">
                            <div class="col-4">
                                <div class="bg-body-secondary rounded py-2 border border-secondary" style="border-radius: 8px !important;">
                                    <div class="text-muted" style="font-size:0.75em;">L1</div>
                                    <div class="fw-bold" id="gh-wb-l1">0 W</div>
                                </div>
                            </div>
                            <div class="col-4">
                                <div class="bg-body-secondary rounded py-2 border border-secondary" style="border-radius: 8px !important;">
                                    <div class="text-muted" style="font-size:0.75em;">L2</div>
                                    <div class="fw-bold" id="gh-wb-l2">0 W</div>
                                </div>
                            </div>
                            <div class="col-4">
                                <div class="bg-body-secondary rounded py-2 border border-secondary" style="border-radius: 8px !important;">
                                    <div class="text-muted" style="font-size:0.75em;">L3</div>
                                    <div class="fw-bold" id="gh-wb-l3">0 W</div>
                                </div>
                            </div>
                        </div>
                    </div>

                </div>
            </div>
        </div>
    </div>';
}
/**
 * Speichert, aktualisiert oder löscht einen Tageseintrag in der SQLite Statistik-Datenbank.
 * Berechnet Autarkie und Eigenverbrauch basierend auf den neuen Werten nach.
 */
function saveDailyStats($date, $data, $action = 'save') {
    $dbPath = '/var/www/html/data/e3dc_stats.db';
    if (!file_exists($dbPath)) return false;

	    try {
	        $db = new PDO('sqlite:' . $dbPath);
	        $db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
	        foreach (['climate_consumption', 'cost_climate'] as $col) {
	            try { $db->exec("ALTER TABLE daily_stats ADD COLUMN $col REAL DEFAULT 0"); } catch (Exception $e) { }
	        }

	        if ($action === 'delete') {
            $stmt = $db->prepare("DELETE FROM daily_stats WHERE date = ?");
            return $stmt->execute([$date]);
        }

        // Autarkie & Eigenverbrauch neu berechnen (für die konsistente Anzeige)
        $pv = (float)str_replace(',', '.', $data['pv_yield'] ?? 0);
        $home = (float)str_replace(',', '.', $data['home_consumption'] ?? 0);
	        $wb = (float)str_replace(',', '.', $data['wb_consumption'] ?? 0);
	        $wb2 = (float)str_replace(',', '.', $data['wb2_consumption'] ?? 0);
	        $wp = (float)str_replace(',', '.', $data['wp_consumption'] ?? 0);
	        $climate = (float)str_replace(',', '.', $data['climate_consumption'] ?? 0);
	        $grid_in = (float)str_replace(',', '.', $data['grid_in'] ?? 0);
        $grid_out = (float)str_replace(',', '.', $data['grid_out'] ?? 0);
        $bat_in = (float)str_replace(',', '.', $data['bat_in'] ?? 0);
        $bat_out = (float)str_replace(',', '.', $data['bat_out'] ?? 0);

	        $totalCons = $home + $wb + $wb2 + $wp + $climate;
        $autarky = ($totalCons > 0) ? max(0, min(100, (($totalCons - $grid_in) / $totalCons) * 100)) : 0;
        $self_con = ($pv > 0) ? max(0, min(100, (($pv - $grid_out) / $pv) * 100)) : 0;

        // Perform an UPDATE if the row exists to avoid wiping new columns like cost_total, saved_u etc.
        $stmtEx = $db->prepare("SELECT COUNT(*) FROM daily_stats WHERE date = ?");
        $stmtEx->execute([$date]);
        $exists = $stmtEx->fetchColumn() > 0;

	        if ($exists) {
	            $updateSql = "UPDATE daily_stats SET pv_yield=?, home_consumption=?, grid_in=?, grid_out=?, bat_in=?, bat_out=?, wb_consumption=?, wb2_consumption=?, wp_consumption=?, climate_consumption=?, autarky=?, self_con=? WHERE date=?";
	            $stmt = $db->prepare($updateSql);
	            return $stmt->execute([$pv, $home, $grid_in, $grid_out, $bat_in, $bat_out, $wb, $wb2, $wp, $climate, $autarky, $self_con, $date]);
	        } else {
	            $cols = "date, pv_yield, home_consumption, grid_in, grid_out, bat_in, bat_out, wb_consumption, wb2_consumption, wp_consumption, climate_consumption, autarky, self_con";
	            $placeholders = "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?";
	            $params = [$date, $pv, $home, $grid_in, $grid_out, $bat_in, $bat_out, $wb, $wb2, $wp, $climate, $autarky, $self_con];

            $stmt = $db->prepare("INSERT INTO daily_stats ($cols) VALUES ($placeholders)");
            return $stmt->execute($params);
        }

    } catch (Exception $e) {
        error_log("saveDailyStats Error: " . $e->getMessage());
        return false;
    }
}

/**
 * Gibt den relativen Pfad für AJAX Requests zurück (Sicherheits-Wrapper).
 */
function getAjaxActionUrl($page) {
    return "index.php?seite=$page&ajax=1";
}

/**
 * Pfadkandidaten für Release- und Git-Metadaten.
 */
function getFooterInstallRootCandidates() {
    $paths = function_exists('getInstallPaths') ? getInstallPaths() : [];
    $installPath = !empty($paths['valid']) ? rtrim($paths['install_path'], '/') : '';
    $sourceRoot = e3dcValidatedProductRoot(dirname(__DIR__));
    $candidates = [
        $installPath,
        $sourceRoot ?: '',
    ];

    $unique = [];
    foreach ($candidates as $candidate) {
        if (!$candidate) continue;
        $candidate = rtrim($candidate, '/');
        if ($candidate === '' || isset($unique[$candidate])) continue;
        $unique[$candidate] = $candidate;
    }
    return array_values($unique);
}

function e3dcReadReleaseVersionFile($file) {
    if (!is_string($file) || $file === '' || !is_file($file) || is_link($file) || !is_readable($file)) {
        return '';
    }
    $size = @filesize($file);
    if ($size === false || $size < 1 || $size > 64) return '';
    $version = trim((string)@file_get_contents($file));
    return preg_match('/^[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9._-]*$/', $version) ? $version : '';
}

/**
 * Liest die installierte Produktversion aus dem validierten Release-Baum.
 * Das Webroot-Duplikat ist nur ein fail-closed Fallback für Altinstallationen.
 */
function readInstalledVersion($productRoots = null, $webrootVersionFile = '/var/www/html/VERSION') {
    $roots = $productRoots === null ? getFooterInstallRootCandidates() : (array)$productRoots;
    foreach ($roots as $root) {
        $validatedRoot = e3dcValidatedProductRoot($root);
        if ($validatedRoot === null) continue;
        $version = e3dcReadReleaseVersionFile($validatedRoot . '/VERSION');
        if ($version !== '') return $version;
    }
    return e3dcReadReleaseVersionFile($webrootVersionFile);
}

function getInstalledReleaseInfo() {
    $info = [
        'version' => readInstalledVersion(),
        'date' => null,
        'date_raw' => null,
    ];
    $policyFiles = [];
    foreach (getFooterInstallRootCandidates() as $root) {
        $policyFiles[] = $root . '/UPDATE_POLICY.json';
    }
    $policyFiles[] = '/var/www/html/UPDATE_POLICY.json';

    foreach ($policyFiles as $file) {
        if (!is_readable($file)) continue;
        $policy = @json_decode((string)@file_get_contents($file), true);
        if (!is_array($policy)) continue;
        if ($info['version'] === '' && !empty($policy['version'])) {
            $info['version'] = trim((string)$policy['version']);
        }
        $rawDate = trim((string)($policy['release_date'] ?? ''));
        if ($rawDate !== '') {
            $ts = strtotime($rawDate . ' 12:00:00');
            $info['date_raw'] = $rawDate;
            $info['date'] = $ts ? date('d.m.Y', $ts) : $rawDate;
            return $info;
        }
    }

    return $info;
}

function renderE3dcModalThemeStyles() {
    static $rendered = false;
    if ($rendered) return '';
    $rendered = true;
    return '
    <style>
    .e3dc-modal-close { opacity: 0.85; }
    html[data-bs-theme="dark"] .e3dc-modal-close,
    body[data-bs-theme="dark"] .e3dc-modal-close,
    body[data-theme="dark"] .e3dc-modal-close {
        filter: invert(1) grayscale(100%) brightness(200%);
    }
    .e3dc-log-body { background: var(--bs-body-bg); }
    .e3dc-log-pre,
    .e3dc-changelog-pre {
        font-family: monospace;
        font-size: 0.8rem;
        color: var(--bs-body-color);
        background: var(--bs-body-bg);
        white-space: pre-wrap;
        margin: 0;
    }
    .e3dc-log-pre { min-height: 52vh; }
    .e3dc-log-terminal { color: var(--bs-success-text-emphasis); }
    html[data-bs-theme="dark"] .e3dc-log-body,
    body[data-bs-theme="dark"] .e3dc-log-body,
    body[data-theme="dark"] .e3dc-log-body,
    html[data-bs-theme="dark"] .e3dc-log-pre,
    body[data-bs-theme="dark"] .e3dc-log-pre,
    body[data-theme="dark"] .e3dc-log-pre,
    html[data-bs-theme="dark"] .e3dc-changelog-pre,
    body[data-bs-theme="dark"] .e3dc-changelog-pre,
    body[data-theme="dark"] .e3dc-changelog-pre {
        background: #050505;
        color: #d1d5db;
    }
    html[data-bs-theme="dark"] .e3dc-log-terminal,
    body[data-bs-theme="dark"] .e3dc-log-terminal,
    body[data-theme="dark"] .e3dc-log-terminal {
        color: #7CFC7C;
    }
    </style>';
}

/**
 * Liest den Git-Hash direkt aus .git. Die WebUI darf für die Footer-Anzeige
 * keinen sudo/git-Shellpfad brauchen, weil Wrapper-only-Systeme das blocken.
 */
function getGitCommitInfo() {
    foreach (getFooterInstallRootCandidates() as $repoDir) {
        if (!is_dir($repoDir . '/.git')) continue;

        $headFile = $repoDir . '/.git/HEAD';
        $cacheFile = '/var/www/html/ramdisk/git_commit_info_cache.json';
        $headContentForCache = is_readable($headFile) ? trim((string)@file_get_contents($headFile)) : '';
        $cacheKeyParts = [$repoDir, $headContentForCache];
        if ($headContentForCache !== '' && strpos($headContentForCache, 'ref:') === 0) {
            $refPath = trim(substr($headContentForCache, 4));
            $refFile = $repoDir . '/.git/' . $refPath;
            $cacheKeyParts[] = is_readable($refFile) ? trim((string)@file_get_contents($refFile)) : '';
        } else {
            $cacheKeyParts[] = $headContentForCache;
        }
        $cacheKey = sha1(implode('|', $cacheKeyParts));
        if (is_readable($cacheFile)) {
            $cached = @json_decode((string)@file_get_contents($cacheFile), true);
            if (is_array($cached)
                && ($cached['key'] ?? '') === $cacheKey
                && (time() - (int)($cached['ts'] ?? 0)) < 3600
                && !empty($cached['info']['hash'])
            ) {
                return $cached['info'];
            }
        }

        $commitHash = null;
        $commitTime = null;
        if (!is_readable($headFile)) continue;
        $headContent = trim((string)@file_get_contents($headFile));
        if (strpos($headContent, 'ref:') === 0) {
            $refPath = trim(substr($headContent, 4));
            $refFile = $repoDir . '/.git/' . $refPath;
            if (is_readable($refFile)) {
                $commitHash = trim((string)@file_get_contents($refFile));
            }
            if (!$commitHash) {
                $packedRefsFile = $repoDir . '/.git/packed-refs';
                $packedRefs = is_readable($packedRefsFile)
                    ? @file($packedRefsFile, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES)
                    : false;
                if (is_array($packedRefs)) {
                    foreach ($packedRefs as $line) {
                        $line = trim((string)$line);
                        if ($line === '' || $line[0] === '#' || $line[0] === '^') continue;
                        $parts = preg_split('/\s+/', $line, 2);
                        if (count($parts) === 2
                            && $parts[1] === $refPath
                            && preg_match('/^[0-9a-f]{7,40}$/i', $parts[0])
                        ) {
                            $commitHash = $parts[0];
                            break;
                        }
                    }
                }
            }
        } else {
            $commitHash = $headContent;
        }

        if (!$commitHash || strlen($commitHash) < 7) continue;
        $info = [
            'hash' => substr($commitHash, 0, 7),
            'date' => $commitTime ? date('d.m.Y', $commitTime) : null,
            'time' => $commitTime ? date('H:i', $commitTime) : null,
        ];
        @file_put_contents($cacheFile, json_encode(['key' => $cacheKey, 'ts' => time(), 'info' => $info], JSON_UNESCAPED_SLASHES));
        return $info;
    }

    return null;
}

function renderFooterVersion() {
    $info = getGitCommitInfo();
    $release = getInstalledReleaseInfo();
    $version = $release['version'] ?? '';
    $date = $release['date'] ?: ($info['date'] ?? null);
    $time = ($release['date'] ? null : ($info['time'] ?? null));

    if ($info) {
        $label = 'A9x ' . htmlspecialchars($info['hash'])
               . ($date ? ' vom ' . htmlspecialchars($date) : '');
        if ($time) $label .= ' um ' . htmlspecialchars($time);
        if ($version) $label .= ' (v' . htmlspecialchars($version) . ')';
        return ' | <a href="https://github.com/A9xxx/Install-E3DC-Control/commits/main"'
             . ' target="_blank" class="text-decoration-none text-secondary"'
             . ' title="Commit-Verlauf auf GitHub">' . $label . '</a>';
    }

    if ($version) {
        $label = 'A9xxx'
               . ($date ? ' vom ' . htmlspecialchars($date) : '')
               . ' (v' . htmlspecialchars($version) . ')';
        return ' | <a href="https://github.com/A9xxx/Install-E3DC-Control/tree/main"'
             . ' target="_blank" class="text-decoration-none text-secondary"'
             . ' title="Zum Quellcode (main branch)">' . $label . '</a>';
    }

    return '';
}

?>
