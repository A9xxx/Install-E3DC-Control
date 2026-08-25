<?php
if (!defined('VIEW_MODE')) {
    define('VIEW_MODE', 'mobile');
}
if (session_status() === PHP_SESSION_NONE) { session_start(); }
require_once 'helpers.php';
handleWebLogin();
sendNoCacheHeaders();
handleVersionCheck(__FILE__);
handleUpdatePreparation();
handleUpdateCheck();
handleServiceRestart();
handleFixPermissions();
handleWatchdogStatus();
handleWatchdogLog();
handleEnergyManagerLog();
handleHAManagerLog();
handleSelfUpdateCheck();
handleRunSelfUpdate();
handleSaveSetting();
handleEnergyFlowLayout();
handleRunUpdate();
handleDailyStats();
handleForceSocUpdate();
handleSystemLog();

// Logik einbinden (Config, Forecast, Preise)
require_once 'logic.php';

// Luxtronik Global Toggle Handler
if (isset($_POST['save_lux_global'])) {
    requireWebAuth(false);
    e3dcRequireCsrfToken(false);
    $paths = getInstallPaths();
    $val = isset($_POST['lux_active']) ? '1' : '0';

    if (!saveE3dcConfigValue('luxtronik', $val)) {
        http_response_code(500);
        echo errorMessage(
            'Luxtronik-Einstellung nicht gespeichert',
            'Die bestehende Konfiguration blieb unverändert; der Dienst wurde nicht neu gestartet. '
            . 'Bitte führe im Installationscenter einmal „Rechte reparieren“ aus und versuche es erneut.'
        );
        exit;
    }
    if (!e3dcRemoveConfigCacheFailClosed('/var/www/html/ramdisk/e3dc_config_cache.json')) {
        http_response_code(500);
        echo errorMessage('Luxtronik-Einstellung gespeichert, Cache nicht entfernt', 'Der Dienst wurde nicht neu gestartet. Bitte führe „Rechte reparieren“ aus.');
        exit;
    }
    $restart = e3dcRestartEnergyManagerFromWeb($paths);
    if (empty($restart['success'])) {
        http_response_code(500);
        echo errorMessage(
            'Luxtronik-Einstellung gespeichert, Dienstneustart nicht bestätigt',
            (string)($restart['message'] ?? 'Bitte Dienststatus prüfen und den Neustart erneut auslösen.')
        );
        exit;
    }
    header("Location: mobile.php?seite=config");
    exit;
}

$historyFiles = getHistoryBackupFiles();

// Luxtronik History Files scannen
$luxtronikFiles = [];
$luxPath = '/var/www/html/tmp/luxtronik_archive/';
if (is_dir($luxPath)) {
    $files = glob($luxPath . 'luxtronik_*.json');
    if($files) {
        rsort($files);
        foreach ($files as $f) {
            if (preg_match('/luxtronik_(\d{4}-\d{2}-\d{2})\.json/', basename($f), $m)) {
                $date = $m[1];
                // Dummy-Dateiname für das History-Dropdown
                $luxtronikFiles[] = ['file' => 'history_' . $date . '.txt', 'label' => date('d.m.Y', strtotime($date))];
            }
        }
    }
}

// Wir versuchen, die statische awattardebug.23.txt zu laden, damit der Graph nicht immer bei "jetzt" beginnt.
// Wir übernehmen die MwSt ($awmwst oder $mwst) aus der logic.php, falls vorhanden.
$vatToUse = isset($awmwst) ? $awmwst : (isset($mwst) ? $mwst : 0);
$staticData = loadStaticPriceData($vatToUse);
$useStaticData = false;
if ($staticData) {
    $priceHistory = $staticData['prices'];
    $priceStartHour = $staticData['start_hour'];
    $priceInterval = $staticData['interval'];
    $useStaticData = true;
    echo "<!-- Static Data Loaded from: " . htmlspecialchars($staticData['source']) . " -->";
}

$seite = $_GET['seite'] ?? 'live';
if ($seite === 'charging') {
    $seite = !empty($wbEnabled) ? 'wallbox' : 'live';
}
$isDocker = e3dcIsDockerEnvironment();

$protectedPages = ['config', 'wallbox', 'waermepumpe'];
if (in_array($seite, $protectedPages) && !isWebAuthenticated()) {
    $seite = 'lock';
}
?>
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>E3DC Mobile Pro</title>
    <link href="assets/vendor/bootstrap/css/bootstrap.min.css" rel="stylesheet">
    <link rel="manifest" href="<?= getAssetUrl('manifest_mobile.json') ?>">
    <script src="assets/vendor/chart.js/chart.umd.min.js"></script>
    <script src="assets/vendor/hammerjs/hammer.min.js"></script>
    <script src="assets/vendor/chartjs-plugin-zoom/chartjs-plugin-zoom.min.js"></script>

    <style>
        :root {
            --bg-body: #0b0e14; --text-body: #f8fafc;
            --bg-card: #1a1f29; --border-card: #2d3748;
            --bg-nav: #1a1f29; --text-muted: #94a3b8;
            --chart-line: rgba(255,255,255,0.5);
            --chart-overlay: rgba(0,0,0,0.2);
        }
        [data-theme="light"] {
            --bg-body: #eef2f6; --text-body: #334155;
            --bg-card: #f8fafc; --border-card: #cbd5e1;
            --bg-nav: #f8fafc; --text-muted: #64748b;
            --chart-line: rgba(0,0,0,0.3);
            --chart-overlay: rgba(0,0,0,0.05);
        }
        body { background-color: var(--bg-body); color: var(--text-body); font-family: -apple-system, sans-serif; transition: background-color 0.3s, color 0.3s; }
        /* Globaler Hover-Effekt für Desktop-Nutzer (Finger-Cursor) */
        button, .nav-item, .btn, [onclick], .fill-bar { cursor: pointer; }
        .dashboard-card { background: var(--bg-card); border: 1px solid var(--border-card); border-radius: 20px; padding: 18px; position: relative; overflow: hidden; height: 100%; transition: background 0.3s, border-color 0.3s; }
        .fill-bar { position: absolute; top: 0; left: 0; height: 100%; transition: width 1.5s ease-in-out, background 0.5s; z-index: 1; opacity: 0.18; }
        .card-content { position: relative; z-index: 2; text-align: center; }
        .label { font-size: 0.7rem; color: var(--text-muted); font-weight: 600; letter-spacing: 0.05em; margin-bottom: 4px; }
        .value { font-size: 1.8rem; font-weight: 900; line-height: 1.2; }
        .unit { font-size: 0.8rem; color: var(--text-muted); font-weight: bold; }
        .nav-item { color: var(--text-muted); text-decoration: none; font-size: 0.75rem; text-align: center; flex: 0 0 auto; min-width: 48px; transition: transform 0.3s, color 0.3s; }
        .nav-item i { display: block; font-size: 1.25rem; margin-bottom: 4px; }
        .nav-item.active { color: #3b82f6; font-weight: bold; }

        .bg-nav-custom { background: var(--bg-nav); border: 1px solid var(--border-card); }
        .mobile-nav-toggle-icon { color: var(--text-body); font-size: 1.25rem; line-height: 1; }
        .mobile-nav-close { filter: none; opacity: 0.85; }
        [data-theme="dark"] .mobile-nav-close { filter: invert(1) grayscale(100%) brightness(200%); }

        /* Landscape / Auto-Rotate gilt nur für echte Tablet-/Mobilbreiten. */
        @media (max-width: 1200px) and (orientation: landscape) {
            body {
                overflow-x: hidden;
                display: flex;
                flex-direction: row;
                align-items: flex-start;
            }
            .container {
                padding-left: 100px !important;
                padding-top: 10px !important;
                max-width: none !important;
                margin: 0 !important;
                flex: 1;
            }
            .mobile-nav {
                position: fixed !important;
                left: 10px;
                top: 10px;
                bottom: 10px;
                width: 80px;
                height: calc(100vh - 20px);
                flex-direction: column;
                justify-content: flex-start;
                align-items: center;
                border-radius: 20px;
                margin: 0 !important;
                padding: 15px 5px;
                overflow-y: auto;
                overflow-x: hidden;
            }
            .nav-item {
                margin-bottom: 15px;
                width: 100%;
                /* transform: rotate(90deg); Icons drehen für ergonomische Ansicht - ENFERNT AUF KUNDENWUNSCH */
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
            }
            .nav-item i { margin-bottom: 2px; }
            #diagramContainer { min-height: 80vh; }
            .flow-container { height: 85vh; }
        }
        .pv-val { color: #fbbf24; } .home-val { color: #3b82f6; } .wb-val { color: #a855f7; } .wp-val { color: #f97316; }
        @keyframes pulse-dynamic { 0% { opacity: 0.18; } 50% { opacity: var(--pulse-intensity, 0.4); } 100% { opacity: 0.18; } }
        .pulse-active { animation: pulse-dynamic var(--pulse-speed, 2s) infinite ease-in-out; }
        .price-ultra-cheap { text-shadow: 0 0 10px rgba(16, 185, 129, 0.8); }
        #price-chart { position: absolute; bottom: 0; left: 0; width: 100%; height: 100%; pointer-events: none; z-index: 1; color: var(--text-muted); }
        #price-line { position: absolute; top: 0; bottom: 0; width: 1px; border-left: 1px dashed var(--chart-line); z-index: 2; pointer-events: none; display: none; }
        #price-line-day { position: absolute; top: 0; bottom: 0; width: 1px; border-left: 1px dotted rgba(255,255,255,0.3); z-index: 1; pointer-events: none; display: none; }
        #price-line-yesterday { position: absolute; top: 0; bottom: 0; width: 1px; border-left: 1px dotted rgba(255,255,255,0.3); z-index: 1; pointer-events: none; display: none; }
        #price-overlay-tomorrow { position: absolute; top: 0; bottom: 0; right: 0; background: var(--chart-overlay); z-index: 0; pointer-events: none; display: none; }
        #price-label-tomorrow { position: absolute; top: 5px; right: 5px; color: rgba(255,255,255,0.3); font-size: 0.7rem; font-weight: bold; display: none; pointer-events: none; }
        #price-label-yesterday { position: absolute; top: 5px; left: 5px; color: rgba(255,255,255,0.3); font-size: 0.7rem; font-weight: bold; display: none; pointer-events: none; }
        #price-time-label { position: absolute; bottom: 4px; transform: translateX(-50%); color: white; font-size: 10px; font-weight: bold; z-index: 3; pointer-events: none; opacity: 0.9; white-space: nowrap; display: none; text-shadow: 1px 1px 2px black; }
        #price-val-min { position: absolute; top: 12px; left: 15px; z-index: 5; font-size: 1.1rem; font-weight: bold; text-align: left; line-height: 1.1; pointer-events: none; text-shadow: 0 1px 2px rgba(0,0,0,0.8); }
        #price-val-max { position: absolute; top: 12px; right: 15px; z-index: 5; font-size: 1.1rem; font-weight: bold; text-align: right; line-height: 1.1; pointer-events: none; text-shadow: 0 1px 2px rgba(0,0,0,0.8); }
        @keyframes blinker { 50% { opacity: 0.2; } }
        .blink-extreme { animation: blinker 0.8s linear infinite; }
        #val-pv-forecast .unit { color: inherit; }

        /* Desktop-spezifische Layout-Korrekturen */
        .mode-desktop .mobile-nav-header { display: none; }
        .mode-desktop .dashboard-card { margin-bottom: 20px; box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.3); }
        .mode-desktop #diagramContainer { display: block !important; }
        .mode-desktop #toggleDiagramBtn { display: none; }
        /* Korrektur für den Status-Kreis im Stop-Feld auf Desktop */
        .mode-desktop .status-kreis { float: left; margin-right: 15px; position: static; }

        /* Energiefluss Styles (Mobile angepasst) */
        .flow-container { background-color: var(--bg-card); border: 1px solid var(--border-card); border-radius: 20px; position: relative; display: flex; flex-direction: column; width: 100%; height: 400px; overflow: hidden; font-family: sans-serif; margin-bottom: 10px; }
        .flow-canvas { position: relative; flex: 1 1 auto; min-width: 0; min-height: 0; width: 100%; overflow: hidden; }
        .flow-container.flow-has-wb2 { height: 520px; }
        .flow-container.flow-has-wb2.flow-has-hs { height: 560px; }
        .flow-container.flow-has-consumption-aggregate { min-height: 540px; }
        .flow-svg { position: absolute; top: 0; left: 0; width: 100%; height: 100%; z-index: 1; pointer-events: none; }
        .flow-line { fill: none; stroke-width: 3; opacity: 0.2; }
        .flow-dots { fill: none; stroke-width: 5; stroke-dasharray: 0 20; animation: flowAnim 1s linear infinite; stroke-linecap: round; }
        @keyframes flowAnim { to { stroke-dashoffset: -60; } }
        .flow-dots.reverse { animation-direction: reverse; }
        .flow-dots.stopped { animation-play-state: paused; opacity: 0; }
        .flow-editor-toolbar { position: relative; z-index: 40; flex: 0 0 auto; display: flex; align-items: flex-start; justify-content: flex-end; flex-wrap: wrap; gap: 5px; width: 100%; min-height: 45px; padding: 6px 8px; border-bottom: 1px solid var(--border-card); }
        .flow-save-status { flex: 1 1 auto; align-self: center; min-width: 0; color: var(--text-muted); font-size: 0.72rem; font-weight: 700; }
        .flow-save-status.is-success { color: #22c55e; }
        .flow-save-status.is-error { color: #ef4444; }
        .flow-editor-controls { display: none; flex: 1 1 100%; min-width: 0; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 5px; max-width: 100%; padding: 0; overflow-x: auto; }
        .flow-container.flow-editing .flow-editor-controls { display: flex; }
        .flow-container.flow-editing .flow-node[data-flow-node] { cursor: grab; outline: 2px dashed rgba(255,255,255,0.42); outline-offset: 3px; }
        .flow-container.flow-editing .flow-node.flow-selected { outline-style: solid; outline-color: #fff; }
        .flow-container.flow-editing .external-wr-lock-btn { pointer-events: none; }
        .flow-container.flow-saving .flow-canvas { pointer-events: none; }
        .flow-color-select { width: auto; max-width: 100px; }
        .flow-color-input { width: 32px; height: 31px; padding: 2px; }
        .flow-label-input { width: 118px; }
        .flow-drag-handle { display: none; position: absolute; right: -10px; top: -10px; width: 28px; height: 28px; padding: 0; align-items: center; justify-content: center; border: 1px solid #94a3b8; border-radius: 50%; background: #111827; color: #f8fafc; box-shadow: 0 4px 12px rgba(0,0,0,.35); touch-action: none; user-select: none; z-index: 8; }
        .flow-container.flow-editing .flow-drag-handle { display: inline-flex; }
        .flow-node .flow-secondary-label { opacity: .78; font-size: .48rem; }
        .flow-node.node-aggregate { width: 60px; height: 60px; border-style: double; }
        .flow-node.node-aggregate .fa-icon { font-size: 1.1rem; }
        .flow-node.node-aggregate .val { font-size: 0.72rem; }
        .flow-container.flow-has-generation-aggregate .node-center,
        .flow-container.flow-has-consumption-aggregate .node-center { width: 54px; height: 54px; }
        .flow-container.flow-has-generation-aggregate .flow-node:not(.node-center):not(.node-aggregate):not(.node-bat):not(.node-grid),
        .flow-container.flow-has-consumption-aggregate .flow-node:not(.node-center):not(.node-aggregate):not(.node-bat):not(.node-grid) { width: 54px; height: 54px; }
        .flow-container.flow-has-generation-aggregate .flow-node .label,
        .flow-container.flow-has-consumption-aggregate .flow-node .label { max-width: 46px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .flow-node { position: absolute; transform: translate(-50%, -50%); border-radius: 50%; display: flex; flex-direction: column; align-items: center; justify-content: center; z-index: 10; box-sizing: border-box; padding: clamp(2px, 0.8vw, 5px); background: var(--bg-card); border: 3px solid; width: 80px; height: 80px; text-align: center; line-height: 1.02; transition: background 0.3s, border-color 0.3s; }
        .flow-node.flow-dragging { transition: none !important; }
        .flow-node > .fa-icon { flex: 0 0 auto; max-width: calc(100% - 8px); font-size: clamp(0.9rem, 4.4vw, 1.4rem); margin-bottom: 2px; }
        .flow-node > .val { display: block; width: calc(100% - 4px); overflow: hidden; white-space: nowrap; font-size: clamp(0.58rem, 2.8vw, 0.86rem); line-height: 1.05; font-weight: bold; font-variant-numeric: tabular-nums; }
        .flow-node > .label { display: block; width: calc(100% - 6px); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: clamp(0.42rem, 1.9vw, 0.58rem); line-height: 1.05; color: var(--text-muted); text-transform: uppercase; }
        .flow-node .flow-pv-split { max-width: 70px; margin-top: 1px; font-size: 0.46rem; line-height: 1.02; color: inherit; opacity: 0.82; text-transform: none; overflow: hidden; text-overflow: ellipsis; }
        .flow-zero-export-badge { position: absolute; top: -10px; right: -39px; display: inline-flex; align-items: center; justify-content: center; gap: 3px; min-width: 62px; height: 19px; padding: 0 5px; border: 1px solid transparent; border-radius: 999px; color: #fff; font-size: 0.48rem; font-weight: 800; line-height: 1; letter-spacing: 0; white-space: nowrap; z-index: 3; box-shadow: 0 3px 9px rgba(15,23,42,0.28); cursor: help; }
        .flow-zero-export-badge[hidden] { display: none !important; }
        .flow-zero-export-badge.is-confirmed { background: #15803d; border-color: #4ade80; }
        .flow-zero-export-badge.is-settling { background: #a16207; border-color: #facc15; }
        .flow-zero-export-badge.is-violation { background: #b91c1c; border-color: #f87171; }
        .flow-container.flow-editing .flow-zero-export-badge { pointer-events: none; }
        .flow-node.node-external-pv { width: 68px; height: 68px; border-width: 2px; }
        .flow-node.node-external-pv .fa-icon { font-size: 1.05rem; margin-bottom: 1px; }
        .flow-node.node-external-pv .val { font-size: 0.72rem; }
        .flow-node.node-external-pv .label { max-width: 52px; font-size: 0.44rem; line-height: 1.02; }
        .flow-node.node-external-pv.is-producing { animation: externalWrPulse 2.4s ease-in-out infinite; }
        .flow-node.node-external-pv.is-manual-locked { border-color: #dc3545 !important; color: #dc3545 !important; box-shadow: 0 0 16px rgba(220,53,69,0.42) !important; }
        .flow-node.node-external-pv.is-price-locked { border-color: #f59e0b !important; color: #f59e0b !important; box-shadow: 0 0 14px rgba(245,158,11,0.34) !important; }
        @keyframes externalWrPulse { 0%, 100% { box-shadow: 0 0 10px rgba(34,197,94,0.28); } 50% { box-shadow: 0 0 20px rgba(34,197,94,0.62); } }
        #f-external-pv-lock { position: absolute; top: 10%; right: 16%; font-size: 0.52rem; }
        .external-wr-lock-btn { position: absolute; right: -7px; bottom: -7px; display: inline-flex; align-items: center; justify-content: center; width: 25px; height: 25px; padding: 0; border: 1px solid var(--border-card); border-radius: 50%; background: var(--bg-card); color: var(--text-main); z-index: 2; }
        .external-wr-lock-btn[aria-pressed="true"] { background: #dc3545; border-color: #dc3545; color: #fff; }
        .flow-node .price-tag { position: absolute; bottom: -20px; background: var(--bg-nav); padding: 2px 6px; border-radius: 8px; font-size: 0.7rem; font-weight: bold; border: 1px solid var(--border-card); white-space: nowrap; }
        .flow-hover-panel { position: absolute; z-index: 45; display: none; min-width: 210px; max-width: min(280px, calc(100% - 20px)); padding: 10px 12px; border-radius: 8px; border: 1px solid var(--border-card); background: rgba(15, 23, 42, 0.94); color: #f8fafc; box-shadow: 0 14px 34px rgba(0,0,0,0.32); pointer-events: none; text-align: left; font-size: 0.76rem; line-height: 1.25; backdrop-filter: blur(12px); }
        .flow-hover-panel.is-visible { display: block; }
        .flow-hover-title { display: flex; justify-content: space-between; gap: 10px; align-items: baseline; font-weight: 800; margin-bottom: 6px; }
        .flow-hover-now { color: #38bdf8; white-space: nowrap; }
        .flow-hover-meta { display: flex; justify-content: space-between; gap: 8px; color: rgba(226,232,240,0.78); margin-bottom: 7px; }
        .flow-hover-bar { display: flex; overflow: hidden; height: 10px; border-radius: 5px; background: rgba(148,163,184,0.18); margin-bottom: 7px; }
        .flow-hover-seg { min-width: 3px; height: 100%; }
        .flow-hover-row { display: flex; justify-content: space-between; gap: 10px; align-items: center; margin-top: 4px; }
        .flow-hover-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 5px; }
        .flow-hover-note { margin-top: 6px; color: rgba(226,232,240,0.68); }
        .flow-node-back {
            position: absolute; transform: translate(-50%, -50%); border-radius: 50%;
            z-index: 5; background-color: var(--bg-card);
            width: 80px; height: 80px;
            top: 15%; left: 50%;
        }
        .node-pv { border-color: #ffc107; color: #ffc107; box-shadow: 0 0 15px rgba(255,193,7,0.3); top: 25%; left: 20%; }
        .node-grid { border-color: #6c757d; color: #ced4da; box-shadow: 0 0 15px rgba(108,117,125,0.3); top: 75%; left: 20%; }
        .node-bat { border-color: #dc3545; color: #dc3545; box-shadow: 0 0 15px rgba(220,53,69,0.3); top: 15%; left: 50%; }
        .node-bat.charging { border-color: #2ecc71; color: #2ecc71; box-shadow: 0 0 15px rgba(46,204,113,0.3); }
        .node-home { border-color: #0dcaf0; color: #0dcaf0; box-shadow: 0 0 15px rgba(13,202,240,0.3); top: 24%; left: 76%; }
        .node-wb { border-color: #2ecc71; color: #2ecc71; box-shadow: 0 0 15px rgba(46,204,113,0.3); top: 72%; left: 76%; }
        .node-wp { border-color: #f97316; color: #f97316; box-shadow: 0 0 15px rgba(249,115,22,0.32); top: 48%; left: 76%; }
        .node-hs { border-color: #fd7e14; color: #fd7e14; box-shadow: 0 0 15px rgba(253,126,20,0.3); }
        .node-climate { border-color: #38bdf8; color: #38bdf8; box-shadow: 0 0 15px rgba(56,189,248,0.32); }
        .node-wp.boost { border-color: #dc3545; color: #dc3545; box-shadow: 0 0 20px rgba(220,53,69,0.6); }
        .node-center { background: transparent; border: none; box-shadow: none; width: 70px; height: 70px; top: 50%; left: 50%; padding: 0; }
        .node-center img { width: 100%; height: 100%; border-radius: 50%; box-shadow: 0 0 20px #0d6efd; }

        @media (orientation: portrait) {
            .forecast-separator { display: block; margin: 5px 0; }
            .forecast-separator i { transform: rotate(90deg); margin: 0; padding: 0; }
        }

        /* Batterie SOC-Gradient: reagiert sofort auf Theme-Wechsel via --bs-body-bg */
        #f-node-bat {
            background: linear-gradient(to top,
                var(--bat-fill, transparent) var(--bat-soc, 0%),
                var(--bs-body-bg) var(--bat-soc-top, 10%)
            ) !important;
        }

        .mobile-flow-switch {
            display: flex;
            justify-content: center;
            gap: 6px;
            padding: 5px;
            margin-bottom: 10px;
            border: 1px solid var(--border-card);
            border-radius: 16px;
            background: var(--bg-nav);
        }
        .mobile-flow-switch button {
            flex: 1;
            border: 0;
            border-radius: 12px;
            padding: 9px 10px;
            font-size: 0.82rem;
            font-weight: 800;
            color: var(--text-muted);
            background: transparent;
        }
        .mobile-flow-switch button.active {
            color: #061016;
            background: linear-gradient(135deg, #22d3ee, #2ecc71);
            box-shadow: 0 0 18px rgba(34, 211, 238, 0.28);
        }
        .mobile-storage-strip {
            display: none;
            padding: 12px 14px;
            border-color: rgba(56, 189, 248, 0.28);
            cursor: pointer;
            background:
                radial-gradient(circle at 12% 0%, rgba(129, 140, 248, 0.16), transparent 42%),
                linear-gradient(135deg, rgba(15, 23, 42, 0.18), rgba(14, 165, 233, 0.06)),
                var(--bg-card);
        }
        .mobile-storage-strip:focus-visible {
            outline: 2px solid #0ea5e9;
            outline-offset: 3px;
        }
        .mobile-storage-strip.storage-active {
            border-color: rgba(34, 211, 238, 0.42);
            box-shadow: 0 10px 28px rgba(14, 165, 233, 0.10);
        }
        .mobile-storage-strip.storage-free {
            border-color: rgba(34, 197, 94, 0.30);
        }
        .mobile-storage-head,
        .mobile-storage-main,
        .mobile-storage-chips,
        .mobile-storage-anchors {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .mobile-storage-head {
            justify-content: space-between;
            margin-bottom: 6px;
        }
        .mobile-storage-kicker {
            color: #a78bfa;
            font-size: 0.68rem;
            font-weight: 800;
            letter-spacing: 0.05em;
            text-transform: uppercase;
        }
        .mobile-storage-state-pill,
        .mobile-storage-chip {
            border: 1px solid rgba(148, 163, 184, 0.22);
            border-radius: 999px;
            background: rgba(15, 23, 42, 0.28);
            color: var(--text-body);
            white-space: nowrap;
        }
        .mobile-storage-state-pill {
            padding: 3px 8px;
            color: #67e8f9;
            font-size: 0.7rem;
            font-weight: 800;
        }
        .mobile-storage-main {
            justify-content: space-between;
            margin-bottom: 7px;
        }
        .mobile-storage-title {
            min-width: 0;
            font-size: 1rem;
            font-weight: 800;
            color: var(--text-body);
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .mobile-storage-soll {
            color: var(--text-muted);
            font-size: 0.78rem;
            font-weight: 700;
            white-space: nowrap;
        }
        .mobile-storage-chips {
            flex-wrap: wrap;
            gap: 6px;
            margin-bottom: 7px;
        }
        .mobile-storage-chip {
            padding: 3px 8px;
            font-size: 0.72rem;
            font-weight: 750;
        }
        .mobile-storage-chip.curve { color: #38bdf8; border-color: rgba(56, 189, 248, 0.35); }
        .mobile-storage-chip.ifc { color: #c4b5fd; border-color: rgba(167, 139, 250, 0.35); }
        .mobile-storage-chip.ems { color: #86efac; border-color: rgba(34, 197, 94, 0.32); }
        [data-theme="light"] .mobile-storage-kicker { color: #5b21b6; }
        [data-theme="light"] .mobile-storage-state-pill {
            color: #075985;
            background: rgba(14, 165, 233, 0.10);
            border-color: rgba(14, 116, 144, 0.34);
        }
        [data-theme="light"] .mobile-storage-chip.curve {
            color: #075985;
            background: rgba(14, 165, 233, 0.10);
            border-color: rgba(14, 116, 144, 0.34);
        }
        [data-theme="light"] .mobile-storage-chip.ifc {
            color: #5b21b6;
            background: rgba(124, 58, 237, 0.10);
            border-color: rgba(109, 40, 217, 0.34);
        }
        [data-theme="light"] .mobile-storage-chip.ems {
            color: #166534;
            background: rgba(22, 163, 74, 0.10);
            border-color: rgba(21, 128, 61, 0.34);
        }
        .mobile-storage-anchors {
            justify-content: space-between;
            color: var(--text-muted);
            font-size: 0.7rem;
            font-weight: 700;
            margin: 6px 0 7px;
        }
        .mobile-storage-anchor {
            min-width: 0;
            display: flex;
            flex-direction: column;
            gap: 1px;
        }
        .mobile-storage-anchor b {
            color: var(--text-body);
            font-size: 0.74rem;
        }
        .mobile-storage-reason {
            color: var(--text-muted);
            font-size: 0.72rem;
            line-height: 1.25;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
        }
        .energy-ring-card {
            --ring-pv: #2ecc71;
            --ring-grid-export: #c4f43d;
            --ring-bat-in: #fbbf24;
            --ring-bat-out: #38bdf8;
            --ring-home: #cbd5e1;
            --ring-wp: #f97316;
            --ring-climate: #a78bfa;
            --ring-wb: #f472b6;
            --ring-wb2: #2dd4bf;
            position: relative;
            min-height: 0;
            padding-bottom: 18px;
            overflow: hidden;
            border: 1px solid var(--border-card);
            border-radius: 24px;
            background:
                radial-gradient(circle at 50% 44%, rgba(34, 211, 238, 0.08), transparent 44%),
                linear-gradient(145deg, rgba(255,255,255,0.04), transparent 46%),
                var(--bg-card);
            box-shadow: inset 0 0 0 1px rgba(255,255,255,0.02), 0 14px 35px rgba(0,0,0,0.22);
        }
        .energy-ring-title {
            position: relative;
            z-index: 4;
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 14px;
            padding: 20px 20px 0;
        }
        .energy-ring-title h3 {
            margin: 0 0 10px 0;
            font-size: 1.45rem;
            font-weight: 760;
        }
        .energy-ring-vehicle {
            display: none;
            color: #22d3ee;
            font-size: 1.05rem;
            font-weight: 500;
        }
        .energy-ring-stage {
            position: relative;
            width: min(86vw, 360px);
            height: min(86vw, 360px);
            margin: 6px auto 0 auto;
        }
        .energy-ring-svg {
            position: absolute;
            inset: 0;
            width: 100%;
            height: 100%;
            filter: drop-shadow(0 0 12px rgba(0,0,0,0.35));
        }
        .energy-ring-svg path {
            fill: none;
            shape-rendering: geometricPrecision;
            transition: stroke 0.25s ease, opacity 0.25s ease;
        }
        .energy-ring-track {
            stroke: rgba(148, 163, 184, 0.34);
            stroke-width: 26;
            stroke-linecap: butt;
        }
        .energy-ring-track.input { stroke: rgba(46, 204, 113, 0.18); }
        .energy-ring-track.output { stroke: rgba(148, 163, 184, 0.34); }
        .energy-ring-track.soc {
            stroke: rgba(56, 189, 248, 0.14);
            stroke-width: 5;
            stroke-linecap: round;
        }
        .energy-ring-arc { stroke-width: 28; stroke-linecap: butt; opacity: 0.96; }
        .energy-ring-arc-soc {
            stroke: #38bdf8;
            stroke-width: 5;
            stroke-linecap: round;
            filter: drop-shadow(0 0 6px rgba(56,189,248,0.58));
        }
        .energy-ring-arc-pv { stroke: var(--ring-pv); filter: drop-shadow(0 0 10px rgba(46,204,113,0.72)); }
        .energy-ring-arc-grid-import { stroke: #f43f5e; filter: drop-shadow(0 0 8px rgba(244,63,94,0.58)); }
        .energy-ring-arc-grid-export { stroke: var(--ring-grid-export); filter: drop-shadow(0 0 8px rgba(196,244,61,0.58)); }
        .energy-ring-arc-bat-in { stroke: var(--ring-bat-in); filter: drop-shadow(0 0 8px rgba(251,191,36,0.58)); }
        .energy-ring-arc-bat-out { stroke: var(--ring-bat-out); filter: drop-shadow(0 0 8px rgba(56,189,248,0.58)); }
        .energy-ring-arc-home { stroke: var(--ring-home); }
        .energy-ring-arc-wp { stroke: var(--ring-wp); filter: drop-shadow(0 0 7px rgba(249,115,22,0.52)); }
        .energy-ring-arc-climate { stroke: var(--ring-climate); filter: drop-shadow(0 0 7px rgba(167,139,250,0.52)); }
        .energy-ring-arc-wb { stroke: var(--ring-wb); filter: drop-shadow(0 0 7px rgba(244,114,182,0.52)); }
        .energy-ring-arc-wb2 { stroke: var(--ring-wb2); filter: drop-shadow(0 0 7px rgba(45,212,191,0.52)); }
        .energy-ring-caption { display: none; }
        .energy-ring-content {
            position: absolute;
            inset: 30% 22%;
            z-index: 3;
            display: flex;
            align-items: center;
            justify-content: center;
            text-align: center;
            pointer-events: none;
        }
        .energy-ring-main {
            max-width: 100%;
            font-size: clamp(0.82rem, 3.5vw, 1.08rem);
            font-weight: 560;
            color: var(--text-body);
            margin: 0;
            line-height: 1.22;
            white-space: normal;
            text-shadow: 0 2px 4px rgba(0,0,0,0.45);
        }
        .energy-ring-row {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 7px;
            min-height: 25px;
            font-weight: 540;
            color: var(--text-muted);
        }
        .energy-ring-row i { min-width: 20px; text-align: center; }
        .ring-pv-text { color: var(--ring-pv); font-size: 1.25rem; }
        .ring-pv-stack { display: inline-flex; flex-direction: column; align-items: flex-start; line-height: 1.06; min-width: 0; }
        .ring-pv-detail { display: none; max-width: 220px; margin-top: 2px; font-size: 0.62rem; color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .ring-grid-text.export { color: var(--ring-grid-export); }
        .ring-grid-text.import { color: #f43f5e; }
        .ring-bat-text.charge { color: var(--ring-bat-in); }
        .ring-bat-text.discharge { color: var(--ring-bat-out); }
        .energy-ring-home { color: var(--ring-home); }
        .energy-ring-input-list,
        .energy-ring-output-list {
            display: grid;
            gap: 3px;
        }
        .energy-ring-legend {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px 16px;
            padding: 0 18px;
        }
        .energy-ring-legend-group {
            min-width: 0;
            padding: 8px 10px;
            border: 1px solid rgba(148, 163, 184, 0.18);
            border-radius: 12px;
            background: rgba(15, 23, 42, 0.16);
        }
        .energy-ring-legend-group.is-source {
            border-color: rgba(52, 211, 153, 0.48);
            background: rgba(5, 150, 105, 0.14);
        }
        .energy-ring-legend-group.is-use {
            border-color: rgba(167, 139, 250, 0.48);
            background: rgba(124, 58, 237, 0.14);
        }
        .energy-ring-legend-title {
            margin-bottom: 4px;
            color: var(--text-muted);
            font-size: 0.62rem;
            font-weight: 750;
            letter-spacing: 0.04em;
            text-transform: uppercase;
        }
        .energy-ring-legend-group.is-source .energy-ring-legend-title { color: #a7f3d0; }
        .energy-ring-legend-group.is-use .energy-ring-legend-title { color: #ddd6fe; }
        .energy-ring-legend .energy-ring-row {
            justify-content: flex-start;
            min-width: 0;
            font-size: 0.78rem;
        }
        .energy-ring-legend .energy-ring-row span { min-width: 0; }
        .energy-ring-row.muted-zero {
            color: rgba(148, 163, 184, 0.46);
        }
        .ring-wp-text { color: var(--ring-wp); }
        .ring-climate-text { color: var(--ring-climate); }
        .ring-wb-text { color: var(--ring-wb); }
        .ring-wb2-text { color: var(--ring-wb2); }
        .energy-ring-soc {
            position: static;
            flex: 0 0 auto;
            z-index: 4;
            color: #e0f2fe;
            font-size: 0.82rem;
            font-weight: 750;
            letter-spacing: 0.02em;
            white-space: nowrap;
            padding: 5px 10px;
            border: 1px solid #38bdf8;
            border-radius: 999px;
            background: #0f172a;
            box-shadow: 0 3px 10px rgba(15, 23, 42, 0.28);
        }
        [data-theme="light"] .energy-ring-card {
            --ring-pv: #15803d;
            --ring-grid-export: #6d28d9;
            --ring-bat-in: #b45309;
            --ring-bat-out: #0369a1;
            --ring-home: #334155;
            --ring-wp: #c2410c;
            --ring-climate: #4338ca;
            --ring-wb: #be185d;
            --ring-wb2: #0f766e;
            background:
                radial-gradient(circle at 50% 45%, rgba(14, 165, 233, 0.12), transparent 42%),
                linear-gradient(145deg, rgba(255,255,255,0.75), rgba(226,232,240,0.72)),
                var(--bg-card);
        }
        [data-theme="light"] .energy-ring-main { color: #0f172a; }
        [data-theme="light"] .energy-ring-track { stroke: rgba(100, 116, 139, 0.32); }
        [data-theme="light"] .energy-ring-legend .energy-ring-row { color: #0f172a; font-weight: 650; }
        [data-theme="light"] .energy-ring-legend .ring-pv-text i { color: var(--ring-pv); }
        [data-theme="light"] .energy-ring-legend .ring-grid-text.export i { color: var(--ring-grid-export); }
        [data-theme="light"] .energy-ring-legend .ring-grid-text.import i { color: #be123c; }
        [data-theme="light"] .energy-ring-legend .ring-bat-text.charge i { color: var(--ring-bat-in); }
        [data-theme="light"] .energy-ring-legend .ring-bat-text.discharge i { color: var(--ring-bat-out); }
        [data-theme="light"] .energy-ring-legend .energy-ring-home i { color: var(--ring-home); }
        [data-theme="light"] .energy-ring-legend .ring-wp-text i { color: var(--ring-wp); }
        [data-theme="light"] .energy-ring-legend .ring-climate-text i { color: var(--ring-climate); }
        [data-theme="light"] .energy-ring-legend .ring-wb-text i { color: var(--ring-wb); }
        [data-theme="light"] .energy-ring-legend .ring-wb2-text i { color: var(--ring-wb2); }
        [data-theme="light"] .energy-ring-legend-group.is-source {
            border-color: #15803d;
            background: rgba(220, 252, 231, 0.84);
        }
        [data-theme="light"] .energy-ring-legend-group.is-use {
            border-color: #4338ca;
            background: rgba(238, 242, 255, 0.90);
        }
        [data-theme="light"] .energy-ring-legend-group.is-source .energy-ring-legend-title { color: #14532d; }
        [data-theme="light"] .energy-ring-legend-group.is-use .energy-ring-legend-title { color: #312e81; }
        [data-theme="light"] .energy-ring-soc {
            color: #075985;
            background: #ffffff;
            border-color: #075985;
        }

        /* Tablets und die Desktop-Vorschau nutzen die verfügbare Fläche für lesbare Knoten. */
        @media (min-width: 700px) {
            .flow-node { width: 100px; height: 100px; }
            .flow-node.node-aggregate { width: 100px; height: 100px; }
            .flow-node.node-aggregate .fa-icon { font-size: 1.35rem; }
            .flow-node.node-aggregate .val { font-size: 0.9rem; }
            .flow-container.flow-has-generation-aggregate .node-center,
            .flow-container.flow-has-consumption-aggregate .node-center { width: 70px; height: 70px; }
            .flow-container.flow-has-generation-aggregate .flow-node:not(.node-center):not(.node-aggregate):not(.node-bat):not(.node-grid),
            .flow-container.flow-has-consumption-aggregate .flow-node:not(.node-center):not(.node-aggregate):not(.node-bat):not(.node-grid) { width: 108px; height: 108px; }
            .flow-container.flow-has-generation-aggregate .flow-node .label,
            .flow-container.flow-has-consumption-aggregate .flow-node .label { max-width: calc(100% - 6px); font-size: 0.72rem; letter-spacing: 0; }
            .flow-container.flow-has-consumption-aggregate { min-height: 640px; }
            .flow-node.node-external-pv { width: 102px; height: 102px; }
            .flow-node.node-external-pv .label { max-width: calc(100% - 6px); font-size: 0.70rem; }
            .mobile-storage-state-pill,
            .mobile-storage-chip { padding: 4px 10px; font-size: 0.8rem; }
        }
        @media (min-width: 1201px) {
            body.mode-mobile { display: block; overflow-x: hidden; }
            body.mode-mobile > .container {
                width: min(calc(100% - 32px), 1120px);
                max-width: 1120px !important;
                margin-right: auto !important;
                margin-left: auto !important;
                padding: 12px !important;
            }
            body.mode-mobile .flow-container { height: clamp(500px, 64vh, 620px); }
        }
        .mobile-app-brand {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 2.25rem;
            min-width: 2.25rem;
            height: 2.25rem;
            padding: 0;
            border-radius: 9px;
            overflow: hidden;
        }
        .mobile-app-brand-icon {
            width: 2rem;
            height: 2rem;
            display: block;
            border-radius: 7px;
        }

        @media (max-width: 380px) {
            .energy-ring-stage { width: min(84vw, 330px); height: min(84vw, 330px); margin-top: 4px; }
            .energy-ring-title { padding: 16px 16px 0; }
            .mobile-storage-main { align-items: flex-start; flex-direction: column; gap: 3px; }
            .mobile-storage-anchors { font-size: 0.66rem; }
            .energy-ring-main { font-size: clamp(0.78rem, 3.4vw, 0.95rem); }
            .energy-ring-soc { font-size: 0.72rem; }
            .energy-ring-legend { grid-template-columns: 1fr; gap: 7px; padding: 0 14px; }
            .energy-ring-legend-group { padding: 6px 9px; }
        }

    </style>
    <link rel="stylesheet" href="assets/vendor/fontawesome/css/all.min.css">
    <link rel="stylesheet" href="assets/vendor/bootstrap-icons/bootstrap-icons.min.css">
</head>
<body class="mode-<?php echo VIEW_MODE; ?> frontend-<?= htmlspecialchars($frontendVariant ?? 'classic', ENT_QUOTES) ?> detail-<?= htmlspecialchars($frontendDetailMode ?? 'normal', ENT_QUOTES) ?>" data-theme="<?= $darkMode ? 'dark' : 'light' ?>" data-bs-theme="<?= $darkMode ? 'dark' : 'light' ?>" data-frontend="<?= htmlspecialchars($frontendVariant ?? 'classic', ENT_QUOTES) ?>" data-detail-mode="<?= htmlspecialchars($frontendDetailMode ?? 'normal', ENT_QUOTES) ?>">
<div class="container py-3">
    <div class="mobile-nav-header mb-3">
        <nav class="navbar bg-nav-custom px-3 py-2" style="border-radius: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.2);">
            <a class="navbar-brand mobile-app-brand shadow-none" href="mobile.php" title="Live Dashboard" aria-label="Live Dashboard">
                <img class="mobile-app-brand-icon" src="<?= getAssetUrl('app-icon-192.png') ?>" alt="" width="32" height="32">
                <span class="visually-hidden">E3DC Control Live Dashboard</span>
            </a>
            <button class="navbar-toggler border-0 shadow-none" type="button" data-bs-toggle="offcanvas" data-bs-target="#mobileNavOffcanvas" aria-controls="mobileNavOffcanvas" style="padding: 4px 8px;">
                <i class="fas fa-bars mobile-nav-toggle-icon" aria-hidden="true"></i>
            </button>
        </nav>
    </div>

    <!-- Offcanvas Menü (Schiebemenü) -->
    <div class="offcanvas offcanvas-start" tabindex="-1" id="mobileNavOffcanvas" aria-labelledby="mobileNavOffcanvasLabel" style="background: var(--bg-nav); color: var(--text-body); max-width: 300px;">
        <div class="offcanvas-header border-bottom border-secondary">
            <h5 class="offcanvas-title fw-bold" id="mobileNavOffcanvasLabel"><i class="fas fa-bars text-primary me-2"></i>Menü</h5>
            <button type="button" class="btn-close mobile-nav-close" data-bs-dismiss="offcanvas" aria-label="Close"></button>
        </div>
        <div class="offcanvas-body p-0">
            <div class="list-group list-group-flush fs-6">
                <!-- Live & Action -->
                <a href="mobile.php" class="list-group-item list-group-item-action bg-transparent text-body border-secondary py-3 <?= $seite=='live'?'fw-bold text-primary active-style':'' ?>">
                    <i class="fas fa-home text-primary me-3 text-center" style="width: 20px; font-size: 1.1rem;"></i> Live Dashboard
                </a>
                <?php if ($wbEnabled): ?>
                <a href="mobile.php?seite=wallbox" class="list-group-item list-group-item-action bg-transparent text-body border-secondary py-3 <?= $seite=='wallbox'?'fw-bold text-success active-style':'' ?>">
                    <i class="fas fa-charging-station text-success me-3 text-center" style="width: 20px; font-size: 1.1rem;"></i> Wallbox
                </a>
                <?php endif; ?>
                <a href="mobile.php?seite=fahrzeug" class="list-group-item list-group-item-action bg-transparent text-body border-secondary py-3 <?= $seite=='fahrzeug'?'fw-bold text-info active-style':'' ?>">
                    <i class="fas fa-car text-info me-3 text-center" style="width: 20px; font-size: 1.1rem;"></i> Fahrzeug Info
                </a>
                <?php if ($wpEnabled || $hsEnabled): ?>
                <a href="mobile.php?seite=waermepumpe" class="list-group-item list-group-item-action bg-transparent text-body border-secondary py-3 <?= $seite=='waermepumpe'?'fw-bold text-danger active-style':'' ?>">
                    <i class="fas <?= $wpEnabled ? 'fa-fire text-danger' : 'fa-fire-burner text-warning' ?> me-3 text-center" style="width: 20px; font-size: 1.1rem;"></i> <?= $wpEnabled ? 'Wärmepumpe' : 'Heizstab' ?>
                </a>
                <?php endif; ?>
                <div class="bg-body-secondary py-2 px-3 fw-bold small text-muted text-uppercase mt-2">Diagramme & Statistik</div>
                <a href="mobile.php?seite=forecast" class="list-group-item list-group-item-action bg-transparent text-body border-secondary py-3 <?= $seite=='forecast'?'fw-bold text-primary active-style':'' ?>">
                    <i class="fas fa-chart-area text-secondary me-3 text-center" style="width: 20px; font-size: 1.1rem;"></i> Prognose
                </a>
                <a href="mobile.php?seite=hybrid" class="list-group-item list-group-item-action bg-transparent text-body border-secondary py-3 <?= $seite=='hybrid'?'fw-bold text-primary active-style':'' ?>">
                    <i class="fas fa-chart-pie text-secondary me-3 text-center" style="width: 20px; font-size: 1.1rem;"></i> Hybrid Ansicht
                </a>
                <a href="mobile.php?seite=history" class="list-group-item list-group-item-action bg-transparent text-body border-secondary py-3 <?= $seite=='history'?'fw-bold text-primary active-style':'' ?>">
                    <i class="fas fa-chart-line text-secondary me-3 text-center" style="width: 20px; font-size: 1.1rem;"></i> Verlauf
                </a>
                <a href="mobile.php?seite=langzeit" class="list-group-item list-group-item-action bg-transparent text-body border-secondary py-3 <?= $seite=='langzeit'?'fw-bold text-warning active-style':'' ?>">
                    <i class="fas fa-calendar-alt text-warning me-3 text-center" style="width: 20px; font-size: 1.1rem;"></i> Langzeit-Statistiken
                </a>
                <a href="mobile.php?seite=vitals" class="list-group-item list-group-item-action bg-transparent text-body border-secondary py-3 <?= $seite=='vitals'?'fw-bold text-danger active-style':'' ?>">
                    <i class="fas fa-heartbeat text-danger me-3 text-center" style="width: 20px; font-size: 1.1rem;"></i> Batterie Vitalwerte
                </a>

                <div class="bg-body-secondary py-2 px-3 fw-bold small text-muted text-uppercase mt-2">System</div>
                <?php $confData = loadE3dcConfig(); if (!empty($confData['config']['matter_bridge']) && $confData['config']['matter_bridge'] == '1'): ?>
                <a href="mobile.php?seite=matter" class="list-group-item list-group-item-action bg-transparent text-body border-secondary py-3 <?= $seite=='matter'?'fw-bold text-primary active-style':'' ?>">
                    <i class="fas fa-atom text-primary me-3 text-center" style="width: 20px; font-size: 1.1rem;"></i> Matter Smart Home Bridge
                </a>
                <?php endif; ?>
                <a href="mobile.php?seite=config" class="list-group-item list-group-item-action bg-transparent text-body border-secondary py-3 <?= $seite=='config'?'fw-bold text-secondary active-style':'' ?>">
                    <i class="fas fa-cog text-secondary me-3 text-center" style="width: 20px; font-size: 1.1rem;"></i> Konfiguration
                </a>
            </div>
            <style>
                .list-group-item.active-style { background: var(--bs-body-bg) !important; border-left: 4px solid var(--bs-primary) !important; padding-left: 1rem !important; }
            </style>
        </div>
    </div>

    <?php if ($seite == 'live'): ?>
    <div class="d-flex justify-content-end align-items-center mb-3 px-1">
        <div class="d-flex align-items-center gap-2">
                <?php $confData = loadE3dcConfig(); if (!empty($confData['config']['web_pin'])): ?>
                <?php if (isWebAuthenticated()): ?>
                    <form method="post" class="d-inline">
                        <?= e3dcCsrfInput() ?>
                        <input type="hidden" name="action" value="web_logout">
                        <button type="submit" class="btn btn-link text-secondary border-0 p-0 align-baseline" title="Sperren" aria-label="Sperren">
                            <i class="fas fa-unlock text-success"></i>
                        </button>
                    </form>
                <?php else: ?>
                    <a href="?seite=lock" class="text-secondary" title="Entsperren"><i class="fas fa-lock text-warning"></i></a>
                <?php endif; ?>
            <?php endif; ?>
                <i id="watchdog-icon" class="fas fa-shield-alt text-secondary" style="display:none; font-size: 1.1rem; cursor:pointer;" title="Watchdog" onclick="showWatchdogLog()"></i>
                <span id="m-weather-alert-badge" class="badge rounded-pill bg-body-tertiary text-info border border-info-subtle" style="display:none;" title="Wetter am Anlagenstandort" role="button" tabindex="0" aria-label="Wetterhinweis anzeigen">
                    <i class="fas fa-cloud-sun me-1"></i><span id="m-weather-alert-badge-text">Wetter</span>
                </span>
                <?= renderConnectionBadge() ?>
                <i id="mobile-darkmode-icon" class="fas fa-<?= $darkMode ? 'sun' : 'moon' ?> text-secondary ms-2" style="cursor:pointer;" onclick="toggleDarkMode(this)"></i>
                <span id="theme-save-status" class="small ms-1" role="status" aria-live="polite" aria-atomic="true"></span>
                <span class="badge border border-secondary text-info ms-2" id="live-time" style="background: var(--bg-card);">--:--:--</span>
            </div>
        </div>


    <!-- Notstrom Alert -->
    <div id="m-notstrom-alert" class="alert alert-danger d-flex align-items-center mb-3 shadow pulsating mx-2" style="display:none !important; border-radius: 12px;">
        <i class="fas fa-bolt-lightning fs-3 me-3"></i>
        <div>
            <h6 class="alert-heading fw-bold mb-1">STROMAUSFALL</h6>
            <div class="small">Notstrombetrieb aktiv!</div>
        </div>
    </div>

    <!-- Watchdog Alert -->
    <div id="m-watchdog-alert" class="alert alert-warning d-flex align-items-center mb-3 shadow pulsating mx-2" style="display:none !important; border-radius: 12px; background-color: #ffc107; color: #000;">
        <i class="fas fa-exclamation-triangle fs-3 me-3"></i>
        <div>
            <h6 class="alert-heading fw-bold mb-1">WATCHDOG FAILSAFE</h6>
            <div class="small" id="m-watchdog-alert-text">Ein Kerndienst ist ausgefallen! System arbeitet nativ.</div>
        </div>
    </div>

        <!-- Live Energiefluss (Ersetzt die alten Kacheln) -->

        <!-- Statistik Bar -->
        <div class="dashboard-card mb-2 d-flex justify-content-around align-items-center py-2" onclick="toggleStatsView('mobile')" style="cursor:pointer; background: var(--bg-nav); border-color: var(--border-card);">
            <div class="text-center" title="Autarkie (Momentan)">
                <div class="label m-0">Autarkie (Live)</div>
                <div class="fw-bold text-success" id="m-val-autarky-live" style="font-size: 1.2rem;">--%</div>
            </div>
            <div class="text-center" title="Autarkie (Heute)">
                <div class="label m-0">Autarkie (Tag)</div>
                <div class="fw-bold text-success" id="m-val-autarky-day" style="font-size: 1.2rem;">--%</div>
            </div>
            <div class="text-center" title="Eigenverbrauch (Heute)">
                <div class="label m-0">Eigenverbrauch</div>
                <div class="fw-bold text-warning" id="m-val-selfcon-day" style="font-size: 1.2rem;">--%</div>
            </div>
            <i class="fas fa-chevron-down text-muted" id="m-stats-chevron"></i>
        </div>
        <div id="m-stats-anchor"></div>

        <div class="dashboard-card mobile-storage-strip mb-2" id="m-storage-strip" role="button" tabindex="0" aria-label="Ladekurve anzeigen" title="Ladekurve anzeigen">
            <div class="mobile-storage-head">
                <div class="mobile-storage-kicker"><i class="fas fa-brain me-1"></i>Speicherregelung</div>
                <span class="mobile-storage-state-pill" id="m-storage-mode-pill">--</span>
            </div>
            <div class="mobile-storage-main">
                <div class="mobile-storage-title" id="m-storage-title">--</div>
                <div class="mobile-storage-soll d-none" id="m-storage-soll"></div>
            </div>
            <div class="mobile-storage-chips">
                <span class="mobile-storage-chip curve" id="m-storage-curve">Kurve --</span>
                <span class="mobile-storage-chip ifc" id="m-storage-ifc">Rahmen --</span>
                <span class="mobile-storage-chip ems" id="m-storage-ems">EMS --</span>
            </div>
            <div class="mobile-storage-anchors" id="m-storage-anchors">
                <span class="mobile-storage-anchor"><span>Start</span><b id="m-storage-start">--</b></span>
                <span class="mobile-storage-anchor"><span>Jetzt</span><b id="m-storage-now">--</b></span>
                <span class="mobile-storage-anchor"><span>Freilauf</span><b id="m-storage-release">--</b></span>
            </div>
            <div class="mobile-storage-reason" id="m-storage-reason">--</div>
        </div>

        <div class="mobile-flow-switch" role="tablist" aria-label="Energiefluss-Ansicht">
            <button type="button" id="m-flow-tab-classic" class="active" onclick="setMobileFlowView('classic')" aria-pressed="true">
                <i class="fas fa-project-diagram me-1"></i>Knoten
            </button>
            <button type="button" id="m-flow-tab-ring" onclick="setMobileFlowView('ring')" aria-pressed="false">
                <i class="fas fa-circle-notch me-1"></i>Ring
            </button>
        </div>

        <div id="m-flow-wrapper" class="position-relative mb-2">
            <div id="m-flow-classic-view" class="mobile-flow-view">
                <?= renderEnergyFlow('mobile') ?>
            </div>
            <div id="m-flow-ring-view" class="mobile-flow-view" style="display:none;">
                <div class="energy-ring-card">
                    <div class="energy-ring-title">
                        <div>
                            <h3>Aktuell</h3>
                            <div class="energy-ring-vehicle" id="m-ring-vehicle"></div>
                        </div>
                        <div class="energy-ring-soc" id="m-ring-soc" role="status" aria-live="polite">Speicher: --%</div>
                    </div>
                    <div class="energy-ring-stage">
                        <svg class="energy-ring-svg" viewBox="0 0 360 360" aria-hidden="true">
                            <path class="energy-ring-track soc" pathLength="100" d="M 34 200 A 146 146 0 0 1 326 200"></path>
                            <path id="m-ring-arc-soc" class="energy-ring-arc-soc" pathLength="100" d="M 34 200 A 146 146 0 0 1 326 200" stroke-dasharray="0 100"></path>
                            <path class="energy-ring-track input" pathLength="100" d="M 54 200 A 126 126 0 0 1 306 200"></path>
                            <path class="energy-ring-track output" pathLength="100" d="M 306 200 A 126 126 0 0 1 54 200"></path>
                            <path id="m-ring-arc-pv" class="energy-ring-arc energy-ring-arc-pv" pathLength="100" d="M 54 200 A 126 126 0 0 1 306 200" stroke-dasharray="0 100"></path>
                            <path id="m-ring-arc-bat-out" class="energy-ring-arc energy-ring-arc-bat-out" pathLength="100" d="M 54 200 A 126 126 0 0 1 306 200" stroke-dasharray="0 100"></path>
                            <path id="m-ring-arc-grid-import" class="energy-ring-arc energy-ring-arc-grid-import" pathLength="100" d="M 54 200 A 126 126 0 0 1 306 200" stroke-dasharray="0 100"></path>
                            <path id="m-ring-arc-house" class="energy-ring-arc energy-ring-arc-home" pathLength="100" d="M 306 200 A 126 126 0 0 1 54 200" stroke-dasharray="0 100"></path>
                            <path id="m-ring-arc-wp" class="energy-ring-arc energy-ring-arc-wp" pathLength="100" d="M 306 200 A 126 126 0 0 1 54 200" stroke-dasharray="0 100"></path>
                            <path id="m-ring-arc-climate" class="energy-ring-arc energy-ring-arc-climate" pathLength="100" d="M 306 200 A 126 126 0 0 1 54 200" stroke-dasharray="0 100"></path>
                            <path id="m-ring-arc-wb" class="energy-ring-arc energy-ring-arc-wb" pathLength="100" d="M 306 200 A 126 126 0 0 1 54 200" stroke-dasharray="0 100"></path>
                            <path id="m-ring-arc-wb2" class="energy-ring-arc energy-ring-arc-wb2" pathLength="100" d="M 306 200 A 126 126 0 0 1 54 200" stroke-dasharray="0 100"></path>
                            <path id="m-ring-arc-bat-in" class="energy-ring-arc energy-ring-arc-bat-in" pathLength="100" d="M 306 200 A 126 126 0 0 1 54 200" stroke-dasharray="0 100"></path>
                            <path id="m-ring-arc-grid-export" class="energy-ring-arc energy-ring-arc-grid-export" pathLength="100" d="M 306 200 A 126 126 0 0 1 54 200" stroke-dasharray="0 100"></path>
                        </svg>
                        <div class="energy-ring-caption input">Eingang</div>
                        <div class="energy-ring-caption output">Ausgang</div>
                        <div class="energy-ring-content">
                            <div class="energy-ring-main" id="m-ring-consumption-text">Verbrauch: --</div>
                        </div>
                    </div>
                    <div class="energy-ring-legend">
                        <div class="energy-ring-legend-group is-source" aria-label="Quellen und Netzbezug">
                            <div class="energy-ring-legend-title">Quellen · Zufluss</div>
                            <div class="energy-ring-input-list" id="m-ring-input-list">
                                <div class="energy-ring-row ring-pv-text"><i class="fas fa-solar-panel"></i><span class="ring-pv-stack"><span id="m-ring-pv-text">--</span><span id="m-ring-pv-detail" class="ring-pv-detail"></span></span></div>
                                <div class="energy-ring-row ring-grid-text" id="m-ring-grid-row"><i id="m-ring-grid-icon" class="fas fa-arrow-right"></i><span id="m-ring-grid-text">--</span></div>
                            </div>
                        </div>
                        <div class="energy-ring-legend-group is-use" aria-label="Verwendung, Speicherung und Netzeinspeisung">
                            <div class="energy-ring-legend-title">Verwendung · Abfluss</div>
                            <div class="energy-ring-output-list" id="m-ring-output-list">
                                <div class="energy-ring-row ring-bat-text" id="m-ring-bat-row"><i id="m-ring-bat-icon" class="fas fa-car-battery"></i><span id="m-ring-bat-text">--</span></div>
                                <div class="energy-ring-row energy-ring-home" id="m-ring-home-row"><i class="fas fa-home"></i><span id="m-ring-home-text">--</span></div>
                                <div class="energy-ring-row ring-wp-text" id="m-ring-wp-row"><i class="fas fa-fire"></i><span id="m-ring-wp-text">--</span></div>
                                <div class="energy-ring-row ring-climate-text" id="m-ring-climate-row"><i class="fas fa-snowflake"></i><span id="m-ring-climate-text">--</span></div>
                                <div class="energy-ring-row ring-wb-text" id="m-ring-wb-row"><i class="fas fa-charging-station"></i><span id="m-ring-wb-text">--</span></div>
                                <div class="energy-ring-row ring-wb2-text" id="m-ring-wb2-row"><i class="fas fa-charging-station"></i><span id="m-ring-wb2-text">--</span></div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Mobile Statistics View Panel -->
            <div id="m-stats-view" class="position-absolute top-0 start-0 w-100 h-100 p-3" style="display: none; background: var(--bg-card); border-radius: 20px; z-index: 20; overflow-y: auto; border: 1px solid var(--border-card);">
                <div class="d-flex justify-content-between align-items-center mb-3">
                    <h6 class="fw-bold m-0"><i class="fas fa-chart-pie text-secondary me-2"></i>Tagesstatistik</h6>
                    <button class="btn btn-sm btn-outline-secondary" onclick="toggleStatsView('mobile')"><i class="fas fa-times"></i></button>
                </div>

                <select class="form-select form-select-sm mb-3 bg-body-secondary text-body border-secondary" id="m-stats-history-select" onchange="loadStatsForDate(this.value, 'mobile')">
                    <option value="today">Heute (Live)</option>
                    <?php foreach ($historyFiles as $hf): ?>
                        <option value="<?= htmlspecialchars($hf['file']) ?>"><?= htmlspecialchars($hf['label']) ?></option>
                    <?php endforeach; ?>
                </select>

                <!-- Hidden data carriers for JS -->
                <span id="m-stat-grid-out-total" style="display:none;">0 kWh</span>
                <span id="m-stat-bat-in-total" style="display:none;">0 kWh</span>

                <div class="d-flex justify-content-around mb-3 pb-2 border-bottom border-secondary">
                    <div class="text-center">
                        <div class="label m-0">Autarkie</div>
                        <div class="fw-bold text-success fs-5" id="m-stat-overlay-autarky">--%</div>
                    </div>
                    <div class="text-center">
                        <div id="m-co2-tree" style="font-size: 1.8rem; line-height: 1; transition: all 0.5s ease;">🌱</div>
                        <div>
                            <span id="m-stat-co2-value" class="fw-bold text-success" style="font-size: 0.9rem;">--</span>
                            <span class="text-muted" style="font-size: 0.6rem;">kg CO₂</span>
                        </div>
                    </div>
                    <div class="text-center">
                        <div class="label m-0">Eigenverbrauch</div>
                        <div class="fw-bold text-warning fs-5" id="m-stat-overlay-selfcon">--%</div>
                    </div>
                </div>

                <!-- Energiebilanz Übersicht -->
                <div class="d-flex flex-wrap justify-content-center gap-2 mb-3 pb-2 border-bottom border-secondary" style="font-size: 0.75rem;">
                    <span class="badge bg-body-tertiary text-warning border border-secondary-subtle">☀ <span id="m-stat-mix-pv">--</span> kWh</span>
                    <span class="badge bg-body-tertiary text-info border border-secondary-subtle">📤 <span id="m-stat-mix-feedin">--</span> kWh</span>
                    <span class="badge bg-body-tertiary text-danger border border-secondary-subtle">⚡ <span id="m-stat-mix-grid">--</span> kWh</span>
                    <span class="badge bg-body-tertiary text-success border border-secondary-subtle">🔋↓ <span id="m-stat-mix-bat-in">--</span></span>
                    <span class="badge bg-body-tertiary text-success border border-secondary-subtle">🔋↑ <span id="m-stat-mix-bat">--</span> kWh</span>
                </div>

                <div id="m-detail-card-saved" class="mb-3" style="display:none;">
                    <div class="d-flex justify-content-between align-items-center border-bottom border-secondary pb-1 mb-2">
                        <div class="label text-success m-0"><i class="fas fa-leaf me-1"></i> kWh-Retter</div>
                        <span id="m-stat-saved-total" class="badge bg-success">-- kWh</span>
                    </div>
                    <div class="d-flex justify-content-between small text-muted">
                        <span title="Heute vor der Software-Abregelung gerettete Energie">Abregelung:</span>
                        <span id="m-stat-saved-derating" class="fw-bold text-body">-- kWh</span>
                    </div>
                    <div class="d-flex justify-content-between small text-muted">
                        <span title="Heute oberhalb der Hardware-Wechselrichtergrenze gerettete Energie">AC-Limit:</span>
                        <span id="m-stat-saved-inverter" class="fw-bold text-body">-- kWh</span>
                    </div>
                    <div id="m-stat-saved-alltime-row" class="d-flex justify-content-between small text-muted">
                        <span id="m-stat-saved-alltime-label" title="Seit Start der kWh-Retter-Erfassung insgesamt gerettete Energie">Gesamt gerettet:</span>
                        <span id="m-stat-saved-total-alltime" class="fw-bold text-success">-- kWh</span>
                    </div>
                </div>

                <div class="mb-3">
	                    <div class="d-flex justify-content-between align-items-center border-bottom border-secondary pb-1 mb-2">
	                        <div class="label text-warning m-0"><i class="fas fa-sun me-1"></i> Sonne (PV)</div>
	                        <span id="m-stat-pv-total" class="badge bg-warning text-dark">-- kWh</span>
	                    </div>
	                    <div class="small text-muted fw-bold">Quellen</div>
	                    <div class="d-flex justify-content-between small text-muted"><span>E3DC-PV:</span> <span id="m-stat-pv-e3dc" class="fw-bold text-body">-- kWh (--%)</span></div>
	                    <div class="d-flex justify-content-between small text-muted"><span>Zusatz-WR:</span> <span id="m-stat-pv-external" class="fw-bold text-body">-- kWh (--%)</span></div>
	                    <div class="d-flex justify-content-between small text-muted mb-1" id="m-stat-pv-source-rest-row" style="display:none;"><span>Quellenrest:</span> <span id="m-stat-pv-source-rest" class="fw-bold text-body">-- kWh (--%)</span></div>
	                    <div class="small text-muted fw-bold mt-1">Verwendung</div>
	                    <div class="d-flex justify-content-between small text-muted"><span>Haus:</span> <span id="m-stat-pv-home" class="fw-bold text-body">-- kWh (--%)</span></div>
                    <div class="d-flex justify-content-between small text-muted"><span>Batterie:</span> <span id="m-stat-pv-bat" class="fw-bold text-body">-- kWh (--%)</span></div>
                    <?php if ($wbEnabled): ?>
                    <div class="d-flex justify-content-between small text-muted"><span>Wallbox:</span> <span id="m-stat-pv-wb" class="fw-bold text-body">-- kWh (--%)</span></div>
                    <?php endif; ?>
                    <?php if ($wpEnabled): ?>
                    <div class="d-flex justify-content-between small text-muted"><span>WP:</span> <span id="m-stat-pv-wp" class="fw-bold text-body">-- kWh (--%)</span></div>
                    <?php endif; ?>
                    <div class="d-flex justify-content-between small text-muted"><span>Netz:</span> <span id="m-stat-pv-grid" class="fw-bold text-body">-- kWh (--%)</span></div>
                </div>
                <div class="mb-3">
                    <div class="d-flex justify-content-between align-items-center border-bottom border-secondary pb-1 mb-2">
                        <div class="label text-success m-0"><i class="fas fa-battery-full me-1"></i> Batterie (Entladung)</div>
                        <span id="m-stat-bat-total" class="badge bg-success">-- kWh</span>
                    </div>
                    <div class="d-flex justify-content-between small text-muted"><span>Haus:</span> <span id="m-stat-bat-home" class="fw-bold text-body">-- kWh (--%)</span></div>
                    <?php if ($wbEnabled): ?>
                    <div class="d-flex justify-content-between small text-muted"><span>Wallbox:</span> <span id="m-stat-bat-wb" class="fw-bold text-body">-- kWh (--%)</span></div>
                    <?php endif; ?>
	                    <?php if ($wpEnabled): ?>
	                    <div class="d-flex justify-content-between small text-muted"><span>WP:</span> <span id="m-stat-bat-wp" class="fw-bold text-body">-- kWh (--%)</span></div>
	                    <?php endif; ?>
	                    <div class="d-flex justify-content-between small text-muted"><span>Netz/Verkauf:</span> <span id="m-stat-bat-grid" class="fw-bold text-body">-- kWh (--%)</span></div>
	                </div>
                <div>
                    <div class="d-flex justify-content-between align-items-center border-bottom border-secondary pb-1 mb-2">
                        <div class="label text-danger m-0"><i class="fas fa-network-wired me-1"></i> Netzbezug</div>
                        <span id="m-stat-grid-total" class="badge bg-danger">-- kWh</span>
                    </div>
                    <div class="d-flex justify-content-between small text-muted"><span>Haus:</span> <span id="m-stat-grid-home" class="fw-bold text-body">-- kWh (--%)</span></div>
                    <div class="d-flex justify-content-between small text-muted"><span>Batterie:</span> <span id="m-stat-grid-bat" class="fw-bold text-body">-- kWh (--%)</span></div>
                    <?php if ($wbEnabled): ?>
                    <div class="d-flex justify-content-between small text-muted"><span>Wallbox:</span> <span id="m-stat-grid-wb" class="fw-bold text-body">-- kWh (--%)</span></div>
                    <?php endif; ?>
                    <?php if ($wpEnabled): ?>
                    <div class="d-flex justify-content-between small text-muted"><span>WP:</span> <span id="m-stat-grid-wp" class="fw-bold text-body">-- kWh (--%)</span></div>
                    <?php endif; ?>
                </div>
                <!-- Kosten & Ersparnis Mobile -->
                <div id="m-detail-card-costs" style="display:none; border-top: 1px solid var(--border-card); padding-top: 10px;">
                    <div class="d-flex justify-content-between align-items-center mb-2">
                        <div class="label text-success m-0"><i class="fas fa-euro-sign me-1"></i> Endergebnis</div>
                        <span id="m-stat-result-total" class="badge bg-success">0.00 €</span>
                    </div>
                    <div class="d-flex justify-content-between small mb-1"><span class="text-muted">Netz (Bezug & Einspeisung):</span> <span id="m-stat-cost-total" class="text-danger fw-bold">0.00 €</span></div>
                    <div class="d-flex justify-content-between small mb-2"><span class="text-muted">Summe der Ersparnis:</span> <span id="m-stat-save-total" class="text-info fw-bold">0.00 €</span></div>
                    <div id="m-stat-eeg-row" class="d-flex justify-content-between small mb-1" style="display:none;"><span class="text-muted">EEG-Einspeisevergütung:</span> <span id="m-stat-eeg-total" class="text-success fw-bold">--</span></div>
                    <div id="m-stat-eeg-note" class="small text-muted mb-2" style="display:none; font-size: 0.7rem;"></div>
                    <div id="m-stat-dv-battery-sale-row" class="d-flex justify-content-between small mb-1" style="display:none;" title="Separater Ist-Erlös aus dem Direktvermarktungs-Tagesreport; nicht in das Endergebnis eingerechnet."><span class="text-muted">DV-Batterieverkauf netto:</span> <span id="m-stat-dv-battery-sale" class="text-success fw-bold">—</span></div>
                    <div id="m-stat-dv-battery-sale-note" class="small text-muted mb-2" style="display:none; font-size: 0.7rem;"></div>

                    <div class="row g-2 mb-2">
                        <div class="col-6">
                            <div class="p-2 rounded bg-body-tertiary border border-secondary border-opacity-10">
                                <div class="label m-0 opacity-75" style="font-size: 0.6rem;">Haus (K/E)</div>
                                <div class="small fw-bold"><span id="m-stat-cost-home">0.00</span> / <span id="m-stat-save-home" class="text-info">0.00</span> <span style="font-size: 0.6rem;">€</span></div>
                            </div>
                        </div>
                        <?php if ($wbEnabled): ?>
                        <div class="col-6">
                            <div class="p-2 rounded bg-body-tertiary border border-secondary border-opacity-10">
                                <div class="label m-0 opacity-75" style="font-size: 0.6rem;">WB (K/E)</div>
                                <div class="small fw-bold"><span id="m-stat-cost-wb">0.00</span> / <span id="m-stat-save-wb" class="text-info">0.00</span> <span style="font-size: 0.6rem;">€</span></div>
                            </div>
                        </div>
                        <?php endif; ?>
                        <?php if ($wpEnabled): ?>
                        <div class="col-6">
                            <div class="p-2 rounded bg-body-tertiary border border-secondary border-opacity-10">
                                <div class="label m-0 opacity-75" style="font-size: 0.6rem;">WP (K/E)</div>
                                <div class="small fw-bold"><span id="m-stat-cost-wp">0.00</span> / <span id="m-stat-save-wp" class="text-info">0.00</span> <span style="font-size: 0.6rem;">€</span></div>
                            </div>
                        </div>
                        <?php endif; ?>
                        <div class="col-6">
                            <div class="p-2 rounded bg-body-tertiary border border-secondary border-opacity-10">
                                <div class="label m-0 opacity-75" style="font-size: 0.6rem;">Ø Preis</div>
                                <div class="small fw-bold" id="m-stat-avg-price">0.0 ct</div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <?php if ($showPriceTrend): ?>
        <div class="dashboard-card mb-2" id="card-price" onclick="toggleDiagram('price')" title="Preisverlauf & Kosten anzeigen" style="height: 70px; padding: 0;">
            <svg id="price-chart" preserveAspectRatio="none" viewBox="0 0 240 100"></svg>
            <div id="price-line"></div>
            <div id="price-line-day"></div>
            <div id="price-line-yesterday"></div>
            <div id="price-overlay-tomorrow"></div>
            <div id="price-label-tomorrow">Morgen</div>
            <div id="price-label-yesterday">Gestern</div>
            <div id="price-time-label"></div>
            <div id="price-val-min"></div>
            <div id="price-val-max"></div>
            <div style="position: absolute; top: 8px; width: 100%; text-align: center; z-index: 2; pointer-events: none;">
                <span class="label m-0" style="color: rgba(255,255,255,0.8); font-size: 0.75rem; text-shadow: 0 1px 2px rgba(0,0,0,0.8);"><i class="fas fa-chart-area me-1"></i> Preis-Trend</span>
                <span id="price-trend-icon" style="text-shadow: 0 1px 2px rgba(0,0,0,0.8);"></span>
            </div>
            <div id="val-price" style="display: none;"></div>
        </div>
        <?php endif; ?>

        <div id="diagramContainer" style="display:none;" class="mb-3">
            <div id="diagramControls" class="d-flex justify-content-between align-items-center p-2 flex-wrap gap-2">
                <select class="form-select form-select-sm border-secondary" style="width: auto; max-width: 140px;" id="mobileHistorySelect" onchange="updateDiagramHistory(this.value)">
                    <option value="" selected>Live</option>
                    <?php foreach ($historyFiles as $hf): ?>
                        <option value="<?= htmlspecialchars($hf['file']) ?>"><?= htmlspecialchars($hf['label']) ?></option>
                    <?php endforeach; ?>
                </select>
                <select class="form-select form-select-sm border-secondary" style="width: auto; max-width: 140px; display:none;" id="mobileHistorySelectWP" onchange="updateDiagramHistory(this.value)">
                    <option value="" selected>Live</option>
                    <?php foreach ($luxtronikFiles as $lf): ?>
                        <option value="<?= htmlspecialchars($lf['file']) ?>"><?= htmlspecialchars($lf['label']) ?></option>
                    <?php endforeach; ?>
                </select>
                <div class="btn-group btn-group-sm" role="group" id="liveTimeFilter">
                    <button type="button" class="btn btn-outline-info active" onclick="setLiveHours(6, this)">6h</button>
                    <button type="button" class="btn btn-outline-info" onclick="setLiveHours(12, this)">12h</button>
                    <button type="button" class="btn btn-outline-info" onclick="setLiveHours(24, this)">24h</button>
                    <button type="button" class="btn btn-outline-info" onclick="setLiveHours(48, this)">48h</button>
                </div>
                <div class="d-flex align-items-center gap-2">
                    <button class="btn btn-sm btn-outline-secondary btn-chart-flip" onclick="toggleChartFlip()" title="Werte klappen (Absolutwerte anzeigen)">
                        <i class="fas fa-arrows-alt-v"></i>
                    </button>
                    <span id="diagramStatus" class="small text-info">Live</span>
                    <button id="diagramUpdateBtn" class="btn btn-sm btn-outline-secondary" onclick="updateDiagram()">
                        <i class="fas fa-sync-alt"></i>
                    </button>
                </div>
            </div>
            <div id="diagramDetails" class="text-center mb-2 small text-info fw-bold" style="display:none;"></div>
            <div style="height: 50vh; min-height: 400px; border-radius: 20px; overflow: hidden; border: 1px solid var(--border-card); position: relative;">
                <!-- Live JS Chart Overlay -->
                <div id="liveChartContainer" class="w-100 h-100 position-absolute top-0 start-0 p-2" style="background-color: var(--bg-card); z-index: 10;">
                    <canvas id="liveChartCanvas"></canvas>
                </div>
            </div>
        </div>

    <?php elseif ($seite == 'forecast'): ?>
        <div class="d-flex justify-content-between align-items-center mb-3 px-2">
            <h5 class="m-0 fw-bold">SoC Prognose</h5>
            <div>
                <button class="btn btn-sm btn-outline-secondary btn-chart-flip me-1" onclick="toggleChartFlip()" title="Werte klappen (Absolutwerte anzeigen)">
                    <i class="fas fa-arrows-alt-v"></i>
                </button>
                <button id="forecastUpdateBtn" class="btn btn-sm btn-outline-secondary" onclick="updateForecast()">
                    <i class="fas fa-sync-alt"></i> Update
                </button>
            </div>
        </div>
        <div id="forecast-kwh-summary" class="text-center text-info small fw-bold mb-2" style="display:none; letter-spacing: 0.02em;"></div>
        <div id="pv-forecast-diagnostic-card" class="dashboard-card px-3 py-2 mb-2 small" hidden>
            <div class="d-flex flex-wrap align-items-center justify-content-between gap-2">
                <span class="fw-bold"><i class="fas fa-chart-bar text-info me-1"></i>PV-Prognosediagnose</span>
                <span id="pv-forecast-diagnostic-status" class="badge text-bg-secondary">Noch keine Auswertung</span>
            </div>
            <div class="d-grid gap-1 mt-2 text-body">
                <span title="Typischer absoluter Unterschied je verglichenem 15-Minuten-Fenster">Trefferabweichung: <strong id="pv-forecast-diagnostic-hit">–</strong></span>
                <span title="Positiv bedeutet im Mittel mehr, negativ weniger Ertrag als vorhergesagt">Richtungsversatz: <strong id="pv-forecast-diagnostic-direction">–</strong></span>
                <span title="Quadratische Fehlerwurzel; gewichtet größere Prognosefehler stärker">RMSE: <strong id="pv-forecast-diagnostic-rmse">–</strong></span>
                <span title="Positiv ist besser als der zuletzt vor Ausgabe bekannte Ertrag desselben UTC-Zeitfensters am Vortag">Skill gegen Tagespersistenz: <strong id="pv-forecast-diagnostic-skill">–</strong></span>
                <span title="Gesamtabweichung, gewichtet nach der tatsächlich erzeugten Energie">Energieabweichung: <strong id="pv-forecast-diagnostic-energy">–</strong></span>
                <span title="Anteil der archivierten Prognosefenster mit gültigem Messwert">Abdeckung: <strong id="pv-forecast-diagnostic-coverage">–</strong></span>
            </div>
            <div class="mt-2 text-muted">
                <div id="pv-forecast-diagnostic-sample">Noch keine vergleichbaren Fenster</div>
                <div>Nur Diagnose – ändert keine Regelung und wählt kein Modell aus.</div>
            </div>
            <div id="pv-forecast-diagnostic-contract" class="mt-1 text-warning">
                Punktprognose – kein belegtes P50.
            </div>
            <div id="pv-forecast-diagnostic-horizons" class="mt-1 text-muted">
                Erfassungs-Vorlauf: noch keine revisionsgebundenen Stichproben.
            </div>
        </div>
        <div class="dashboard-card" style="height: calc(100vh - 180px); min-height: 400px; display: flex; flex-direction: column; position: relative;">
            <div id="primaryChartSurface" style="flex: 1; border-radius: 20px; overflow: hidden; position: relative;">
                <!-- Live JS Chart Overlay -->
                <div id="liveChartContainer" class="w-100 h-100 position-absolute top-0 start-0 p-2" style="background-color: var(--bg-card); z-index: 10;">
                    <canvas id="liveChartCanvas"></canvas>
                </div>
            </div>
            <div id="directMarketingForecastSurface" class="p-2" style="display:none; height:250px;">
                <div class="d-flex flex-wrap align-items-center gap-2 small mb-2">
                    <span class="fw-bold" style="color:#8b5cf6;">Direktvermarktung – ausgewählter Fahrplan</span>
                    <span id="directMarketingForecastState" class="text-muted"></span>
                </div>
                <div class="position-relative" style="height:210px;"><canvas id="directMarketingForecastChart"></canvas></div>
            </div>
            <div class="text-center mt-2 small text-muted" id="forecastStatus"></div>
        </div>

    <?php elseif ($seite == 'hybrid'): ?>
        <div class="d-flex justify-content-between align-items-center mb-3 px-2">
            <h5 class="m-0 fw-bold">Hybrid Ansicht</h5>
            <div>
                <button class="btn btn-sm btn-outline-secondary btn-chart-flip me-1" onclick="toggleChartFlip()" title="Werte klappen (Absolutwerte anzeigen)">
                    <i class="fas fa-arrows-alt-v"></i>
                </button>
                <button id="diagramUpdateBtn" class="btn btn-sm btn-outline-secondary" onclick="updateDiagram()">
                    <i class="fas fa-sync-alt"></i> Update
                </button>
            </div>
        </div>

        <div id="diagramContainer" class="mb-3">
            <div id="diagramControls" class="d-flex justify-content-between align-items-center p-2 flex-wrap gap-2 mb-2">
                <select class="form-select form-select-sm border-secondary" style="width: auto; max-width: 140px;" id="mobileHistorySelect" onchange="updateDiagramHistory(this.value)">
                    <option value="" selected>Live</option>
                    <?php foreach ($historyFiles as $hf): ?>
                        <option value="<?= htmlspecialchars($hf['file']) ?>"><?= htmlspecialchars($hf['label']) ?></option>
                    <?php endforeach; ?>
                </select>
                <div class="btn-group btn-group-sm" role="group" id="liveTimeFilter">
                    <button type="button" class="btn btn-outline-info active" onclick="setLiveHours(6, this)">6h</button>
                    <button type="button" class="btn btn-outline-info" onclick="setLiveHours(12, this)">12h</button>
                    <button type="button" class="btn btn-outline-info" onclick="setLiveHours(24, this)">24h</button>
                    <button type="button" class="btn btn-outline-info" onclick="setLiveHours(48, this)">48h</button>
                </div>
            </div>

            <div class="dashboard-card" style="height: calc(100vh - 230px); min-height: 450px; display: flex; flex-direction: column; position: relative;">
                <div id="primaryChartSurface" style="flex: 1; border-radius: 20px; overflow: hidden; position: relative;">
                    <!-- Live JS Chart Overlay -->
                    <div id="liveChartContainer" class="w-100 h-100 position-absolute top-0 start-0 p-2" style="background-color: var(--bg-card); z-index: 10;">
                        <canvas id="liveChartCanvas"></canvas>
                    </div>
                </div>
                <div id="directMarketingForecastSurface" class="p-2" style="display:none; height:250px;">
                    <div class="d-flex flex-wrap align-items-center gap-2 small mb-2">
                        <span class="fw-bold" style="color:#8b5cf6;">Direktvermarktung – ausgewählter Fahrplan</span>
                        <span id="directMarketingForecastState" class="text-muted"></span>
                    </div>
                    <div class="position-relative" style="height:210px;"><canvas id="directMarketingForecastChart"></canvas></div>
                </div>
                <div class="text-center mt-2 small text-muted" id="diagramStatus">Live</div>
            </div>
        </div>

    <?php elseif ($seite == 'matter'): ?>
        <?php include 'matter.php'; ?>
    <?php elseif ($seite == 'lock'): ?>
        <div class="dashboard-card text-center mt-4">
            <div class="card-content p-3">
                <i class="fas fa-lock text-warning mb-3" style="font-size: 3rem;"></i>
                <h4 class="fw-bold mb-3">Geschützter Bereich</h4>
                <p class="text-muted small mb-4">Bitte gib deine PIN ein, um die Steuerung zu entsperren.</p>
                <?php if (isset($_SESSION['login_error'])): ?>
                    <div class="alert alert-danger py-2 small"><?= htmlspecialchars($_SESSION['login_error_message'] ?? 'Falsche PIN. Bitte versuche es erneut.') ?></div>
                    <?php unset($_SESSION['login_error']); ?>
                    <?php unset($_SESSION['login_error_message']); ?>
                <?php endif; ?>
                <form method="post" action="">
                    <?= e3dcCsrfInput() ?>
                    <input type="hidden" name="action" value="web_login">
                    <div class="mb-4">
                        <input type="password" name="pin" class="form-control form-control-lg text-center fw-bold bg-body-secondary text-body border-secondary" placeholder="****" autofocus required style="letter-spacing: 0.5em;">
                    </div>
                    <button type="submit" class="btn btn-warning w-100 rounded-pill fw-bold py-3">Entsperren</button>
                </form>
            </div>
        </div>
    <?php elseif ($seite == 'fahrzeug'): ?>
        <?php include 'fahrzeug.php'; ?>
    <?php elseif ($seite == 'wallbox'): include 'Wallbox.php';
          elseif ($seite == 'vitals'): include 'vitals.php';
          elseif ($seite == 'config'): ?>
        <div class="mb-3">
            <div class="dashboard-card mb-3 p-3">
                <div class="d-flex align-items-center justify-content-between gap-3">
                    <div>
                        <div class="fw-bold text-info"><i class="fas fa-cloud-download-alt me-2"></i>Updates</div>
                        <div class="small text-muted">Web-UI und Systemstand</div>
                    </div>
                    <span class="badge border border-secondary text-secondary"><?= htmlspecialchars(readInstalledVersion() ?: 'V4') ?></span>
                </div>
                <?php if (!$isDocker): ?>
                    <div class="d-grid gap-2 mt-3">
                        <button id="btn-update-installer" class="btn btn-outline-info w-100 py-3 rounded-4 fw-bold shadow-sm" onclick="startInstallerUpdate()" title="Aktualisiert E3DC-Control über den sicheren Systemjob">
                            <i class="fas fa-sync-alt me-2"></i>System Update <span id="update-badge-installer" class="badge bg-danger ms-1" style="display:none;">!</span>
                        </button>
                    </div>
                <?php else: ?>
                    <div class="alert alert-secondary small mb-0 mt-3 rounded-4">
                        <i class="fab fa-docker me-2"></i>Docker-Installationen werden über das Container-Image aktualisiert.
                    </div>
                <?php endif; ?>
            </div>

            <button class="btn btn-outline-info w-100 py-3 rounded-4 border-secondary fw-bold shadow-sm mt-3 btn-diagnose" onclick="showDiagnoseModal()">
                <i class="fas fa-stethoscope me-2"></i>Diagnose
            </button>
            <button class="btn btn-outline-danger w-100 py-3 rounded-4 border-secondary fw-bold shadow-sm mt-3" onclick="restartService()">
                <i class="fas fa-power-off me-2"></i>E3DC-Control Neustart
            </button>
        </div>

        <?php $energyManagerServiceExists = file_exists('/etc/systemd/system/energy_manager.service') || e3dcIsDockerEnvironment(); ?>
        <?php if ($energyManagerServiceExists): ?>
            <div class="dashboard-card mb-3 p-3 d-flex align-items-center justify-content-between">
                <div>
                    <div class="fw-bold text-info"><i class="fas fa-robot me-2"></i>Energy Manager</div>
                    <div class="small text-muted">WP & intelligentes Laden</div>
                </div>
                <form method="post">
                    <?= e3dcCsrfInput() ?>
                    <input type="hidden" name="save_lux_global" value="1">
                    <div class="form-check form-switch m-0">
                        <input class="form-check-input" type="checkbox" name="lux_active" value="1" <?= $luxtronikEnabled ? 'checked' : '' ?> onchange="this.form.submit()" style="transform: scale(1.3);">
                    </div>
                </form>
            </div>
        <?php endif; ?>

        <?php include 'config_editor.php'; ?>
    <?php elseif ($seite === 'waermepumpe' && ($wpEnabled || $hsEnabled)): ?>
        <?php include 'waermepumpe.php'; ?>
    <?php elseif ($seite == 'history'): ?>
        <?php include 'history.php'; ?>
    <?php elseif ($seite == 'langzeit'): ?>
        <?php include 'langzeit.php'; ?>
    <?php endif; ?>

    <!-- Footer -->
    <footer class="text-center text-muted mt-5 pt-3 border-top border-secondary">
        <small>
            E3DC Control &copy; <?= date('Y') ?> |
            <a href="#" class="text-decoration-none text-secondary" data-bs-toggle="modal" data-bs-target="#changelogModal">Changelog</a>
            | <a href="https://www.photovoltaikforum.com/thread/259876-e3dc-control-native-python-ki-prognose-dynamische-stromtarife-wallbox-steuerung/?action=lastPost" target="_blank" class="text-decoration-none text-secondary" title="Zum neuen E3DC-Control V4 Thread im PV-Forum (letzter Beitrag)"><i class="fas fa-comments"></i> PV-Forum</a>
            | <a href="help.php" class="text-decoration-none text-secondary">FAQ</a>
            <br>
            <?= renderFooterVersion() ?>
        </small>
    </footer>
    </div>

<?= renderUpdateModal('modal-dialog-scrollable modal-fullscreen-sm-down') ?>
<?= renderWatchdogModal('modal-dialog-scrollable modal-fullscreen-sm-down') ?>
<?= renderHAModal('modal-dialog-scrollable modal-fullscreen-sm-down') ?>
<?= renderChangelogModal('modal-dialog-scrollable modal-fullscreen-sm-down') ?>
<?= renderDiagnoseModal('modal-dialog-scrollable modal-fullscreen-sm-down') ?>

<!-- Ladekurven-Chart Modal -->
<div class="modal fade" id="storageCurveModal" tabindex="-1" aria-hidden="true">
  <div class="modal-dialog modal-lg modal-dialog-centered modal-dialog-scrollable modal-fullscreen-sm-down">
    <div class="modal-content">
      <div class="modal-header py-2 border-info border-opacity-25">
        <h5 class="modal-title text-info fw-bold"><i class="fas fa-route me-2"></i>Ladekurve <span id="sc-modal-day">Heute</span> <span id="sc-modal-phase" class="badge bg-info bg-opacity-10 text-info border border-info border-opacity-25 ms-2" style="font-size:0.7rem;"></span></h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body p-3">
        <div class="d-flex flex-wrap gap-3 mb-3 small" id="sc-meta-row">
          <span class="text-muted">Aktueller SoC: <span id="sc-current-soc" class="fw-bold text-success">--%</span></span>
          <span class="text-muted">Tagesziel: <span id="sc-target-soc" class="fw-bold text-info">--%</span></span>
          <span class="text-muted">Regelziel: <span id="sc-active-target" class="fw-bold text-success">--</span></span>
          <span class="text-muted">Morgen-Puffer: <span id="sc-morning-target" class="fw-bold text-success">--%</span></span>
          <span class="text-muted">Pre-Dump-Min: <span id="sc-predump-min" class="fw-bold text-success">--%</span></span>
          <span class="text-muted">Pre-Dump-Bedarf: <span id="sc-predump-kwh" class="fw-bold text-info">-- kWh</span></span>
          <span class="text-muted" id="sc-noon-wrap" style="display:none;">Zwischenziele: <span id="sc-noon-target" class="fw-bold text-warning">--%</span></span>
          <span class="text-muted">Max erreichbar: <span id="sc-max-soc" class="fw-bold text-warning">--%</span></span>
          <span id="sc-qratio-wrap" class="text-muted">Kurvenform: <span id="sc-qratio" class="fw-bold">--</span> <i class="fas fa-info-circle" title="Hohe Werte bedeuten: Die Kurve wartet länger auf den eingestellten Freilauf-SoC. Kleine Werte laden früher und direkter."></i></span>
          <span class="text-muted ms-auto">Plan vom: <span id="sc-plan-ts" class="fw-bold">--</span></span>
        </div>
        <div id="sc-standard-chart-wrap">
          <div class="small mb-2">
            <span class="fw-bold text-info">Anlagenregelung – Standard-Ladekurve</span>
            <span class="text-muted ms-2">SoC aus PV, Haus und planbaren Lasten; ohne Direktvermarktungswirkung.</span>
          </div>
          <div style="position:relative; height:260px;">
            <canvas id="storageCurveChart"></canvas>
          </div>
        </div>
        <div id="sc-direct-marketing-chart-wrap" class="mt-3" style="display:none;">
          <div class="d-flex flex-wrap align-items-center gap-2 mb-2 small">
            <span class="fw-bold" style="color:#8b5cf6;"><i class="fas fa-chart-line me-1"></i>Direktvermarktung – ausgewählter Fahrplan</span>
            <span id="sc-direct-marketing-chart-state" class="text-muted"></span>
          </div>
          <div style="position:relative; height:260px;">
            <canvas id="directMarketingTrajectoryChart"></canvas>
          </div>
        </div>
        <div id="sc-direct-marketing-section" class="mt-3 p-2 rounded" style="display:none; background:var(--bs-body-bg); border:1px solid rgba(var(--bs-success-rgb),0.22);">
          <div class="small fw-bold text-success mb-2"><i class="fas fa-coins me-1"></i>Direktvermarktung</div>
          <div id="sc-direct-marketing-summary" class="small"></div>
          <div id="sc-direct-marketing-windows" class="small mt-2"></div>
        </div>
        <div class="mt-3 p-2 rounded" style="background:var(--bs-body-bg); border:1px solid rgba(var(--bs-info-rgb),0.2);">
          <div class="small fw-bold text-info mb-2"><i class="fas fa-info-circle me-1"></i>Was bedeuten die Kurvenwerte?</div>
          <div id="sc-explain-box" class="small"></div>
          <div class="mt-2 small text-muted border-top border-secondary border-opacity-10 pt-2" id="sc-reason-text"></div>
        </div>
      </div>
    </div>
  </div>
</div>

<script src="assets/vendor/jquery/jquery-3.6.0.min.js"></script>
<script src="assets/vendor/bootstrap/js/bootstrap.bundle.min.js"></script>
    <script src="<?= getAssetUrl('solar.js') ?>" defer></script>
<script>
const PV_MAX = <?= $pvMax ?>; const WP_MAX = <?= $wpMax ?>; const BAT_MAX = <?= $maxBatPower ?>; const BAT_CAPACITY = <?= $batteryCapacity ?>; const AVGS = <?= json_encode($avgs) ?>;
const PRICE_HISTORY = <?= json_encode($priceHistory) ?>;
let FORECAST_DATA = <?= json_encode($forecastData) ?>;
const LON = <?= json_encode($lon) ?>;
const PV_STRINGS = <?= json_encode($pvStrings) ?>;
const PV_ATMOSPHERE = <?= json_encode($pvAtmosphere) ?>;
let DARK_MODE = <?= $darkMode ? 'true' : 'false' ?>;
let SHOW_FORECAST = <?= $showForecast ? 'true' : 'false' ?>;
window.E3DC_CSRF_TOKEN = <?= json_encode(e3dcCsrfToken()) ?>;
window.UI_ENERGY_FLOW = <?= json_encode(getEnergyFlowUiConfig(), JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE) ?>;
window.E3DC_PAGE = <?= json_encode($seite) ?>;
const PRICE_START_HOUR = <?= $priceStartHour ?>;
const PRICE_INTERVAL = <?= $priceInterval ?>;
const USE_STATIC_CHART = <?= $useStaticData ? 'true' : 'false' ?>;
let lastUpdateTs = Math.floor(Date.now() / 1000);
let CURRENT_VIEW = 'normal';
let statusCheckInterval = null;

function toggleForecast(el) {
    const previousShowForecast = SHOW_FORECAST;
    SHOW_FORECAST = !SHOW_FORECAST;
    const applyForecastIcon = () => {
        if (!el || !el.classList) return;
        el.classList.toggle('fa-eye', SHOW_FORECAST);
        el.classList.toggle('fa-eye-slash', !SHOW_FORECAST);
    };
    const setStatus = (message, success) => {
        if (typeof showThemeSaveFeedback === 'function') {
            showThemeSaveFeedback(message, success);
            return;
        }
        const status = document.getElementById('theme-save-status');
        if (status) {
            status.textContent = message;
            status.className = 'small ms-1 ' + (success ? 'text-success' : 'text-danger');
            status.hidden = false;
        }
    };
    applyForecastIcon();

    const saveRequest = fetch('mobile.php', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-CSRF-Token': String(window.E3DC_CSRF_TOKEN || '')
        },
        body: 'action=save_setting&key=show_forecast&value=' + (SHOW_FORECAST ? '1' : '0')
    }).then(response => {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.text();
    }).then(result => {
        if (result.trim() !== 'ok') throw new Error('Einstellung wurde nicht bestätigt.');
        setStatus('Prognose gespeichert', true);
        updateDashboard();
    }).catch(() => {
        SHOW_FORECAST = previousShowForecast;
        applyForecastIcon();
        setStatus('Prognose nicht gespeichert – zurückgesetzt', false);
        updateDashboard();
    });
    void saveRequest;
}

function toggleDarkMode(el) {
    const previousDarkMode = DARK_MODE;
    DARK_MODE = !DARK_MODE;
    const theme = DARK_MODE ? 'dark' : 'light';
    document.body.setAttribute('data-theme', theme);
    document.body.setAttribute('data-bs-theme', theme);
    document.documentElement.setAttribute('data-bs-theme', theme); // Bootstrap CSS vars (z.B. --bs-body-bg)
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem('theme', theme); } catch (e) {}
    el.className = DARK_MODE ? 'fas fa-sun text-secondary' : 'fas fa-moon text-secondary';

    const setStatus = (message, success) => {
        if (typeof showThemeSaveFeedback === 'function') {
            showThemeSaveFeedback(message, success);
            return;
        }
        const status = document.getElementById('theme-save-status');
        if (status) {
            status.textContent = message;
            status.className = 'small ms-1 ' + (success ? 'text-success' : 'text-danger');
            status.hidden = false;
        }
    };

    // Speichern
    fetch('mobile.php', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-CSRF-Token': String(window.E3DC_CSRF_TOKEN || '')
        },
        body: 'action=save_setting&key=darkmode&value=' + (DARK_MODE ? '1' : '0')
    }).then(response => {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.text();
    }).then(result => {
        if (result.trim() !== 'ok') throw new Error('Einstellung wurde nicht bestätigt.');
        setStatus('Gespeichert', true);
    }).catch(() => {
        DARK_MODE = previousDarkMode;
        const previousTheme = DARK_MODE ? 'dark' : 'light';
        document.body.setAttribute('data-theme', previousTheme);
        document.body.setAttribute('data-bs-theme', previousTheme);
        document.documentElement.setAttribute('data-bs-theme', previousTheme);
        document.documentElement.setAttribute('data-theme', previousTheme);
        el.className = DARK_MODE ? 'fas fa-sun text-secondary' : 'fas fa-moon text-secondary';
        try { localStorage.setItem('theme', previousTheme); } catch (e) {}
        setStatus('Nicht gespeichert – zurückgesetzt', false);
        window.dispatchEvent(new CustomEvent('themeChanged'));
    });

    // Explizites Event auslösen, damit Diagramme (langzeit.php) reagieren können
    setTimeout(() => {
        window.dispatchEvent(new CustomEvent('themeChanged'));
    }, 50);

    // Refresh currently visible diagram to apply the new theme
    const currentPage = '<?= $seite ?>';
    // WICHTIG: Auch wenn der Container gerade ausgeblendet ist, wollen wir beim nächsten Öffnen
    // das richtige Theme. Aber um Serverlast zu sparen, aktualisieren wir nur, wenn sichtbar
    // ODER wir setzen ein Flag, dass ein Update nötig ist.
    if (currentPage === 'live') {
        updateDiagram();
    } else if (currentPage === 'forecast') {
        updateForecast();
    } else if (currentPage === 'hybrid') {
        updateDiagram();
    } else if (currentPage === 'history' && typeof window.triggerHistoryUpdate === 'function') {
        // history.php is included, its function should be available
        window.triggerHistoryUpdate();
    }
}


let mobileLiveFetchPromise = null;
let mobileLiveFetchController = null;
let mobileLiveRequestGeneration = 0;
const mobileLiveFetchTimeoutMs = 10000;

function invalidateMobileLiveFetch() {
    mobileLiveRequestGeneration += 1;
    if (mobileLiveFetchController) mobileLiveFetchController.abort();
    mobileLiveFetchController = null;
    mobileLiveFetchPromise = null;
}

function updateDashboard() {
    if (typeof e3dcLiveAuthBlocked === 'function' && e3dcLiveAuthBlocked()) return Promise.resolve(null);
    const wsFresh = window.liveWs
        && window.liveWs.readyState === WebSocket.OPEN
        && window.liveWsLastMessageTs
        && (Date.now() - window.liveWsLastMessageTs) < 5000;
    if (wsFresh) return Promise.resolve(null);
    if (mobileLiveFetchPromise) return mobileLiveFetchPromise;

    const requestGeneration = ++mobileLiveRequestGeneration;
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    mobileLiveFetchController = controller;
    let timeoutId = null;
    const timeoutPromise = new Promise((_resolve, reject) => {
        timeoutId = setTimeout(() => {
            if (controller) controller.abort();
            const error = new Error('Live-Anfrage überschritt das Zeitlimit');
            error.name = 'AbortError';
            reject(error);
        }, mobileLiveFetchTimeoutMs);
    });
    const requestPromise = e3dcFetchLiveJson(
        'get_live_json.php?t=' + Date.now(),
        controller ? {signal: controller.signal} : {}
    ).then(res => {
        if (typeof e3dcReadLiveJsonResponse === 'function') return e3dcReadLiveJsonResponse(res);
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
    });
    const trackedPromise = Promise.race([requestPromise, timeoutPromise]).then(data => {
        if (requestGeneration !== mobileLiveRequestGeneration) return;
        if (typeof e3dcClearLiveAuthRecovery === 'function') e3dcClearLiveAuthRecovery();
        if (data) processMobileData(data);
    }).catch(err => {
        if (requestGeneration !== mobileLiveRequestGeneration) return;
        if (err && err.name === 'AbortError') return;
        if (typeof e3dcHandleLiveAuthFailure === 'function' && e3dcHandleLiveAuthFailure(err)) return;
        console.error("Fetch Live JSON Error:", err);
        const statusBadge = document.getElementById('connection-status');
        if (statusBadge) {
            statusBadge.className = 'badge rounded-pill bg-danger text-body';
            statusBadge.innerText = 'Offline';
        }
    }).finally(() => {
        if (timeoutId !== null) clearTimeout(timeoutId);
        if (mobileLiveFetchPromise === trackedPromise) mobileLiveFetchPromise = null;
        if (mobileLiveFetchController === controller) mobileLiveFetchController = null;
    });
    mobileLiveFetchPromise = trackedPromise;
    return trackedPromise;
}

let mobileLivePollTimer = null;
let mobileLivePollGeneration = 0;
let mobileLiveTransportStarted = false;
let mobileLiveLastResumeMs = 0;
const mobileWebSocketEnabled = false;
function mobileLivePollDelayMs() {
    return document.hidden ? 10000 : 2000;
}
function scheduleMobileLivePoll(immediate = false) {
    const generation = ++mobileLivePollGeneration;
    if (mobileLivePollTimer) clearTimeout(mobileLivePollTimer);
    function tickMobileLivePoll() {
        Promise.resolve(updateDashboard()).finally(() => {
            if (generation !== mobileLivePollGeneration) return;
            mobileLivePollTimer = setTimeout(tickMobileLivePoll, mobileLivePollDelayMs());
        });
    }
    if (immediate) {
        tickMobileLivePoll();
    } else {
        mobileLivePollTimer = setTimeout(tickMobileLivePoll, mobileLivePollDelayMs());
    }
}
function resumeMobileLiveTransport() {
    if (document.hidden) return false;
    if (!mobileLiveTransportStarted) return startMobileLiveTransportOnce();
    const now = Date.now();
    if ((now - mobileLiveLastResumeMs) < 500) return true;
    mobileLiveLastResumeMs = now;
    invalidateMobileLiveFetch();
    scheduleMobileLivePoll(true);
    return true;
}
document.addEventListener('visibilitychange', function() {
    if (!mobileLiveTransportStarted) return;
    if (document.hidden) scheduleMobileLivePoll(false);
    else resumeMobileLiveTransport();
});
window.addEventListener('pageshow', resumeMobileLiveTransport);
window.addEventListener('focus', resumeMobileLiveTransport);
window.addEventListener('online', resumeMobileLiveTransport);

function initWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
    window.liveWs = new WebSocket(protocol + window.location.host + '/ws');
    window.liveWs.onmessage = function(e) {
        try {
            const data = JSON.parse(e.data);
            window.liveWsLastMessageTs = Date.now();
            if (data) processMobileData(data);
        } catch (err) {
            console.error("WebSocket Message Error:", err);
        }
    };
    window.liveWs.onclose = function() { window.liveWsLastMessageTs = 0; setTimeout(initWebSocket, 3000); };
    window.liveWs.onerror = function() { window.liveWsLastMessageTs = 0; window.liveWs.close(); };
}

// startSystemUpdate, pollUpdate, finalize entfernt -> jetzt in solar.js

function placeMobileStatsPanel() {
    const anchor = document.getElementById('m-stats-anchor');
    const panel = document.getElementById('m-stats-view');
    if (!anchor || !panel || panel.parentElement === anchor) return;
    panel.classList.remove('position-absolute', 'top-0', 'start-0', 'w-100', 'h-100');
    panel.classList.add('dashboard-card', 'p-3', 'mb-2');
    panel.style.zIndex = '';
    panel.style.overflowY = 'visible';
    anchor.appendChild(panel);
}

placeMobileStatsPanel();

// Solar wird mit defer geladen. Der Live-Transport startet genau einmal, nachdem
// processMobileData vollständig bereitsteht; frühe Pakete können so nicht verloren gehen.
function startMobileLiveTransportOnce() {
    if (mobileLiveTransportStarted) return true;
    if (typeof window.processMobileData !== 'function') return false;
    mobileLiveTransportStarted = true;
    mobileLiveLastResumeMs = Date.now();
    // Der native WebSocket-Producer bleibt bis zu einem vollständigen
    // Web-Auth-Vertrag für Handshake und Reconnect deaktiviert. Bis dahin
    // läuft genau ein gebündelter, authentifizierter Pollingpfad.
    if (mobileWebSocketEnabled) initWebSocket();
    scheduleMobileLivePoll(true);
    return true;
}
window.addEventListener('e3dc:solar-ready', startMobileLiveTransportOnce, {once: true});
if (window.e3dcSolarScriptReady === true) {
    startMobileLiveTransportOnce();
} else if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', startMobileLiveTransportOnce, {once: true});
} else {
    startMobileLiveTransportOnce();
}

function updateLastUpdateDisplay() {
    if (!lastUpdateTs) return;
    const d = new Date(lastUpdateTs * 1000);
    const timeStr = d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');
    document.getElementById('diagramStatus').innerHTML = timeStr;
}

function toggleDiagram(view = 'normal') {
    let c = document.getElementById('diagramContainer');

    // --- NEU: Statistik-Overlay schließen, falls es geöffnet ist ---
    if (typeof statsViewActive !== 'undefined' && statsViewActive) {
        toggleStatsView('mobile');
    }

    // If diagram is hidden, or we are switching to a different view
    if (c.style.display === 'none' || CURRENT_VIEW !== view) {
        c.style.display = 'block';
        CURRENT_VIEW = view;
        updateDashboard(); // Sofort Details aktualisieren

        // Reset History Select to Live when opening new tile
        const histSelect = document.getElementById('mobileHistorySelect');
        const histSelectWP = document.getElementById('mobileHistorySelectWP');
        if(histSelect) histSelect.value = '';
        if(histSelectWP) histSelectWP.value = '';

        // Toggle correct dropdown
        if (view === 'wp') {
            if(histSelect) histSelect.style.display = 'none';
            if(histSelectWP) histSelectWP.style.display = 'block';
        } else {
            if(histSelect) histSelect.style.display = 'block';
            if(histSelectWP) histSelectWP.style.display = 'none';
        }

        updateDiagramHistory(''); // Reset UI and trigger update
    }
    // If diagram is visible and we click the same tile again, hide it
    else {
        c.style.display = 'none';
    }
}

function setLiveHours(hours, btn) {
    currentLiveHours = hours;
    const group = document.getElementById('liveTimeFilter');
    if (group) {
        group.querySelectorAll('.btn').forEach(b => b.classList.remove('active'));
    }
    if (btn) btn.classList.add('active');
    updateDiagram();
}

function updateDiagramHistory(file) {
    const btnGroup = document.getElementById('liveTimeFilter');
    if (file) {
        if(btnGroup) btnGroup.style.display = 'none';
    } else {
        if(btnGroup) btnGroup.style.display = 'inline-flex';
    }
    updateDiagram();
}

    function updateDiagram() {
        const jsContainer = document.getElementById('liveChartContainer');
        if (jsContainer) jsContainer.style.display = 'block';

        const histSelect = document.getElementById(CURRENT_VIEW === 'wp' ? 'mobileHistorySelectWP' : 'mobileHistorySelect');
        const file = histSelect ? histSelect.value : '';

        const btn = document.getElementById('diagramUpdateBtn');
        const stat = document.getElementById('diagramStatus');
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>'; }
        if (stat) stat.innerText = 'Lade...';

        if (CURRENT_VIEW === 'price') {
            if (typeof loadJsPriceChart === 'function') loadJsPriceChart(currentLiveHours, file);
        } else if ('<?= $seite ?>' === 'hybrid') {
            if (typeof loadJsHybridChart === 'function') loadJsHybridChart(currentLiveHours, file);
        } else {
            if (typeof loadJsLiveChart === 'function') loadJsLiveChart(currentLiveHours, file);
        }

        setTimeout(() => {
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-sync-alt"></i>'; }
            if (stat) stat.innerText = file ? 'Archiv' : 'Live';
        }, 500);
    }

    function updateForecast() {
        const btn = document.getElementById('forecastUpdateBtn');
        const stat = document.getElementById('forecastStatus');
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>'; }
        if (stat) stat.innerText = 'Lade...';

        if (typeof loadJsForecastChart === 'function') loadJsForecastChart('');

        setTimeout(() => {
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-sync-alt"></i> Update'; }
            if (stat) stat.innerText = 'Aktualisiert: ' + new Date().toLocaleTimeString('de-DE');
        }, 500);
    }

// startSystemUpdate, pollUpdate, finalize entfernt -> jetzt in solar.js

// Initialisierung für Desktop-Modus
window.addEventListener('DOMContentLoaded', () => {
    // Forecast Init
    if ('<?= $seite ?>' === 'forecast') {
        const jsContainer = document.getElementById('liveChartContainer');
        if (jsContainer) {
            jsContainer.style.display = 'block';
            if (typeof loadJsForecastChart === 'function') loadJsForecastChart('');
        }
    }

    // Hybrid Init
    if ('<?= $seite ?>' === 'hybrid') {
        const jsContainer = document.getElementById('liveChartContainer');
        if (jsContainer) {
            jsContainer.style.display = 'block';
            if (typeof loadJsHybridChart === 'function') loadJsHybridChart(currentLiveHours, '');
        }
    }



    // restartService entfernt -> jetzt in solar.js

    // Watchdog Status
    function checkWatchdog() {
        const setWatchdogUnknown = function() {
            const icon = document.getElementById('watchdog-icon');
            if (!icon) return;
            icon.style.display = 'inline-block';
            icon.className = 'fas fa-shield-alt text-secondary';
            icon.title = 'Watchdog-Status unbekannt (Abruf fehlgeschlagen)';
            icon.setAttribute('data-watchdog-state', 'unknown');
            icon.setAttribute('aria-label', 'Watchdog-Status unbekannt');
        };
        fetch('mobile.php?action=watchdog_status').then(r => {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
        }).then(data => {
            const icon = document.getElementById('watchdog-icon');
            if (!data || typeof data !== 'object' || !icon) {
                setWatchdogUnknown();
                return;
            }
            if (data.installed) {
                icon.style.display = 'inline-block';
                icon.title = data.message;
                icon.setAttribute('data-watchdog-state', data.warning ? 'warning' : (data.active ? 'active' : 'inactive'));
                if (data.warning) {
                    icon.className = 'fas fa-shield-alt text-warning';
                } else if (data.active) {
                    icon.className = 'fas fa-shield-alt text-success';
                } else {
                    icon.className = 'fas fa-shield-alt text-danger';
                }
            } else {
                icon.style.display = 'none';
            }

            // --- NEU: Diagnose Errors verarbeiten ---
            if (data.diagnose_errors) {
                window.currentDiagnoseErrors = data.diagnose_errors;
                const hasErr = data.diagnose_errors.length > 0;
                document.querySelectorAll('.btn-diagnose').forEach(btn => {
                    if (!btn.dataset.origClass) btn.dataset.origClass = btn.className;
                    if (hasErr) {
                        btn.className = btn.dataset.origClass.replace(/btn-outline-[a-z]+/, 'btn-warning text-dark');
                    } else {
                        btn.className = btn.dataset.origClass;
                    }
                });
                if (typeof updateDiagnoseDropdown === 'function') updateDiagnoseDropdown();
            }
        }).catch(setWatchdogUnknown);
    }
    setInterval(checkWatchdog, 10000);
    checkWatchdog();

    // showWatchdogLog, handleConnectionClick entfernt -> jetzt in solar.js
});
</script>
<script>
  if ('serviceWorker' in navigator) {
    const e3dcSwUrl = '<?= getAssetUrl('sw.js') ?>';
    let e3dcHadController = Boolean(navigator.serviceWorker.controller);

    navigator.serviceWorker.addEventListener('controllerchange', function() {
      if (!e3dcHadController) {
        e3dcHadController = true;
        return;
      }
      const reloadKey = 'e3dc-sw-reload-' + e3dcSwUrl;
      if (sessionStorage.getItem(reloadKey) === 'done') return;
      sessionStorage.setItem(reloadKey, 'done');
      window.location.reload();
    });

    window.addEventListener('load', function() {
      navigator.serviceWorker.register(e3dcSwUrl)
        .then(reg => {
          console.log('Service Worker erfolgreich registriert!', reg);
          if (reg.waiting) {
            reg.waiting.postMessage({type: 'SKIP_WAITING'});
          }
          reg.addEventListener('updatefound', function() {
            const worker = reg.installing;
            if (!worker) return;
            worker.addEventListener('statechange', function() {
              if (worker.state === 'installed' && navigator.serviceWorker.controller) {
                worker.postMessage({type: 'SKIP_WAITING'});
              }
            });
          });
          if (typeof reg.update === 'function') {
            reg.update().catch(() => {});
          }
        })
        .catch(err => console.error('Service Worker Registrierung fehlgeschlagen:', err));
    });
  } else {
    console.log('Service Worker wird von diesem Browser nicht unterstützt.');
  }
</script>
</body>
</html>
