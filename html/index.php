<?php
session_start();
require_once 'helpers.php';
if (!function_exists('e3dcCallHandlerIfAvailable')) {
    function e3dcCallHandlerIfAvailable($handler) {
        if (function_exists($handler)) {
            $handler();
        }
    }
}

// During a self-update, index.php and helpers.php can briefly be from different
// revisions. Optional action handlers must therefore be guarded.
e3dcCallHandlerIfAvailable('handleWebLogin');
e3dcCallHandlerIfAvailable('sendNoCacheHeaders'); // ← Das zwingt den Browser, das HTML immer frisch zu laden
if (function_exists('handleVersionCheck')) {
    handleVersionCheck(__FILE__);
}
e3dcCallHandlerIfAvailable('handleUpdatePreparation');
e3dcCallHandlerIfAvailable('handleUpdateCheck');
e3dcCallHandlerIfAvailable('handleReleaseRollback');
e3dcCallHandlerIfAvailable('handleServiceRestart');
e3dcCallHandlerIfAvailable('handleFixPermissions');
e3dcCallHandlerIfAvailable('handleWatchdogStatus');
e3dcCallHandlerIfAvailable('handleWatchdogLog');
e3dcCallHandlerIfAvailable('handleSelfUpdateCheck');
e3dcCallHandlerIfAvailable('handleRunSelfUpdate');
e3dcCallHandlerIfAvailable('handleEnergyManagerLog');
e3dcCallHandlerIfAvailable('handleHAManagerLog');
e3dcCallHandlerIfAvailable('handleSaveSetting');
e3dcCallHandlerIfAvailable('handleDirectMarketingDashboardAction');
e3dcCallHandlerIfAvailable('handleEnergyFlowLayout');
e3dcCallHandlerIfAvailable('handleRunUpdate');
e3dcCallHandlerIfAvailable('handleDailyStats');
e3dcCallHandlerIfAvailable('handleForceSocUpdate');
e3dcCallHandlerIfAvailable('handleSystemLog');

// Zero-Touch Onboarding Check
if (!file_exists('/var/www/html/data/e3dc_v4.json')) {
    header("Location: install_wizard.php");
    exit;
}

require_once 'logic.php';// Luxtronik Global Toggle Handler
if (isset($_POST['save_lux_global'])) {
    requireWebAuth(false);
    e3dcRequireCsrfToken(false);
    $val = isset($_POST['lux_active']) ? '1' : '0';
    saveE3dcConfigValue('luxtronik', $val);

    if (e3dcIsDockerEnvironment()) {
        $python = getPythonInterpreter();
        $runtimePaths = getInstallPaths();
        $script = !empty($runtimePaths['valid'])
            ? rtrim($runtimePaths['install_path'], '/') . '/Installer/luxtronik/energy_manager.py'
            : '';
        if ($script !== '' && is_file($script)) {
            shell_exec("pkill -f 'energy_manager.py'");
            sleep(1);
            shell_exec("nohup " . escapeshellarg($python) . " " . escapeshellarg($script) . " > /var/www/html/logs/energy_manager.log 2>&1 &");
        }
    } else {
        e3dcRunServiceWrapperAction('restart', ['energy_manager']);
    }
    header("Location: index.php?seite=config");
    exit;
}

$seite = $_GET['seite'] ?? 'dashboard';
if ($seite === 'charging') {
    $seite = !empty($wbEnabled) ? 'wallbox' : 'dashboard';
}
$isDocker = e3dcIsDockerEnvironment();
$nativeWallboxStatusEnabled = hasNativeWallboxStatusConfig($_c ?? []);
$protectedPages = ['config', 'wallbox', 'waermepumpe', 'klima'];

if (in_array($seite, $protectedPages, true) && !isWebAuthenticated()) {
    if (isset($_GET['ajax']) && $_GET['ajax'] == '1') {
        requireWebAuth(true);
    }
    $seite = 'lock';
}

// Clean AJAX interception before any HTML headers are sent
if (isset($_GET['ajax']) && $_GET['ajax'] == '1') {
    $ajaxFile = basename($seite) . '.php';
    if (file_exists($ajaxFile)) {
        require_once $ajaxFile;
    }
    exit;
}

    // Historie aus der ramdisk (Python-nativ) scannen
    $historyFiles = getHistoryBackupFiles();

    // Luxtronik History Files (deaktiviert - Langzeit-Statistik nutzen)
    $luxtronikFiles = [];
?>
<!DOCTYPE html>
<html lang="de" data-bs-theme="<?= $darkMode ? 'dark' : 'light' ?>" data-frontend="<?= htmlspecialchars($frontendVariant ?? 'classic', ENT_QUOTES) ?>" data-detail-mode="<?= htmlspecialchars($frontendDetailMode ?? 'normal', ENT_QUOTES) ?>">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>E3DC Control Dashboard</title>

    <link rel="manifest" href="<?= getAssetUrl('manifest.json') ?>">

    <link href="assets/vendor/bootstrap/css/bootstrap.min.css" rel="stylesheet">
    <link href="assets/vendor/fontawesome/css/all.min.css" rel="stylesheet">
    <script src="assets/vendor/chart.js/chart.umd.min.js"></script>
    <script src="assets/vendor/hammerjs/hammer.min.js"></script>
    <script src="assets/vendor/chartjs-plugin-zoom/chartjs-plugin-zoom.min.js"></script>
    <style>
        /* Dark Mode Defaults (Bootstrap handles most via data-bs-theme="dark") */
        [data-bs-theme="dark"] body { background-color: #121212; color: #e0e0e0; }
        [data-bs-theme="dark"] .card { background-color: #1e1e1e; border-color: #333; box-shadow: 0 4px 10px rgba(0,0,0,0.3); }

        /* Light Mode Overrides */
        [data-bs-theme="light"] body { background-color: #eef2f6; color: #334155; }
        [data-bs-theme="light"] .card { background-color: #f8fafc; border-color: #cbd5e1; box-shadow: 0 4px 6px rgba(0,0,0,0.05); }

        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
        .card { border-radius: 12px; transition: transform 0.2s; }
        /* Hover-Effekt nur für Dashboard-Cards, nicht für Container in Unterseiten */
        .dashboard-view .card:hover { transform: translateY(-2px); border-color: #444; cursor: pointer; }
        .icon-box { width: 56px; height: 56px; display: flex; align-items: center; justify-content: center; border-radius: 16px; font-size: 1.75rem; }
        .val-large { font-size: 2.2rem; font-weight: 700; letter-spacing: -1px; }
        .val-unit { font-size: 1rem; color: #888; font-weight: 400; margin-left: 4px; }
        .tile-kwh-badge { font-size: 0.72rem; line-height: 1.15; white-space: nowrap; }
        .wallbox-card-main { min-width: 0; }
        .wallbox-meta-stack { min-width: 90px; max-width: min(48%, 13rem); }
        .wallbox-car-badge { display: inline-block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; line-height: 1.15; }
        .battery-meta-stack { min-width: 6.4rem; line-height: 1; }
        .battery-icon-box { flex-shrink: 0; }
        .battery-value-row { display: inline-flex; align-items: baseline; gap: 0.58rem; flex-wrap: wrap; min-width: 0; }
        .battery-soc-chip {
            display: inline-flex;
            align-items: baseline;
            min-width: 2.9rem;
            padding-left: 0.58rem;
            border-left: 1px solid var(--bs-border-color-translucent);
            font-size: 1.02rem;
            font-weight: 800;
            line-height: 1;
            text-align: center;
            white-space: nowrap;
            animation: none !important;
            transform: none !important;
        }
        .battery-time-badge { min-width: 6.4rem; min-height: 1.35rem; display: inline-flex; align-items: center; justify-content: center; white-space: nowrap; }
        .battery-time-badge.is-placeholder { visibility: hidden; }
        .pulsating { animation: pulse 2s infinite ease-in-out; }
        @keyframes pulse {
            0% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.6; transform: scale(0.92); }
            100% { opacity: 1; transform: scale(1); }
        }
        .chart-container { height: 500px; width: 100%; overflow: hidden; border-radius: 0 0 12px 12px; }
        .forecast-summary { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 0.45rem 1.35rem; letter-spacing: 0; }
        .forecast-summary-day { display: inline-grid; grid-template-columns: auto minmax(0, 1fr); column-gap: 0.45rem; align-items: start; min-width: min(100%, 12rem); }
        .forecast-summary-label { line-height: 1.28; padding-top: 0.04rem; }
        .forecast-summary-lines { display: flex; flex-direction: column; gap: 0.08rem; min-width: 0; }
        .forecast-summary-line { display: flex; flex-wrap: wrap; gap: 0.35rem 0.75rem; line-height: 1.28; min-width: 0; }
        .forecast-summary-yield, .forecast-summary-consumption { align-items: baseline; }
        .forecast-summary-value { white-space: nowrap; }
        @media (max-width: 575.98px) {
            .forecast-summary { display: grid; grid-template-columns: 1fr; gap: 0.55rem; }
            .forecast-summary-day { width: 100%; grid-template-columns: 6.4rem minmax(0, 1fr); }
        }
        iframe { border: none; width: 100%; height: 100%; }
        .btn-group-custom .btn { border-color: #444; color: #aaa; }
        .btn-group-custom .btn:hover, .btn-group-custom .btn.active { background-color: #333; color: #fff; border-color: #555; }
        .status-badge { font-size: 0.8rem; padding: 0.35em 0.65em; }
        body.detail-compact .tile-detail,
        body.hide-tile-details .tile-detail { display: none !important; }
        .detail-toggle-button { position: relative; line-height: 1; }
        .detail-toggle-button.is-detail::after {
            content: "";
            position: absolute;
            width: 0.42rem;
            height: 0.42rem;
            border-radius: 999px;
            background: #dc3545;
            top: -0.12rem;
            right: -0.22rem;
            box-shadow: 0 0 0 2px var(--bs-body-bg);
        }
        body.detail-compact #dashboard-status-cards {
            --bs-gutter-x: 0.45rem;
            --bs-gutter-y: 0.45rem;
            align-items: center;
            width: 100%;
            margin-left: 0 !important;
            margin-right: 0 !important;
            margin-bottom: 0.75rem !important;
        }
        .dashboard-compact-badge-stack { display: none; }
        body.detail-compact .dashboard-compact-badge-stack {
            --compact-badge-gap: 0.46rem;
            --compact-badge-height: 2.76rem;
            display: grid;
            grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
            align-items: flex-start;
            gap: var(--compact-badge-gap);
            width: 100%;
        }
        .dashboard-compact-badge-column {
            display: grid;
            grid-auto-rows: var(--compact-badge-height);
            align-content: start;
            gap: var(--compact-badge-gap);
            min-width: 0;
        }
        body.detail-compact .dashboard-compact-badge-stack #dashboard-status-cards {
            display: grid;
            grid-auto-rows: var(--compact-badge-height);
            gap: var(--compact-badge-gap);
            margin-bottom: 0 !important;
        }
        body.detail-compact .dashboard-compact-badge-stack #dashboard-status-cards > [class*="col-"] {
            padding-left: 0 !important;
            padding-right: 0 !important;
            width: 100%;
        }
        body.detail-compact #dashboard-status-cards > [class*="col-"] {
            flex: 0 0 auto;
            width: auto;
            max-width: 100%;
        }
        body.detail-compact #dashboard-status-cards .card,
        body.detail-compact .dashboard-consumer-badge .card {
            height: 100%;
            min-height: var(--compact-badge-height);
            width: 100%;
            min-width: 0;
            max-width: 100%;
            border-radius: 999px;
        }
        body.detail-compact #dashboard-status-cards .card-body,
        body.detail-compact .dashboard-consumer-badge .card-body {
            height: 100%;
            min-height: 0 !important;
            padding: 0.34rem 0.62rem !important;
        }
        body.detail-compact #dashboard-status-cards .icon-box,
        body.detail-compact .dashboard-consumer-badge .icon-box {
            width: 1.9rem;
            height: 1.9rem;
            min-width: 1.9rem;
            border-radius: 999px;
            font-size: 0.95rem;
            margin-right: 0.48rem !important;
        }
        body.detail-compact #dashboard-status-cards .val-large,
        body.detail-compact .dashboard-consumer-badge .val-large {
            font-size: 1.03rem;
            letter-spacing: 0;
            white-space: nowrap;
        }
        body.detail-compact #dashboard-status-cards .val-unit,
        body.detail-compact .dashboard-consumer-badge .val-unit {
            font-size: 0.68rem;
            margin-left: 2px;
        }
        body.detail-compact #dashboard-status-cards .text-end,
        body.detail-compact #dashboard-status-cards .battery-meta-stack,
        body.detail-compact #dashboard-status-cards .battery-soc-chip,
        body.detail-compact #dashboard-status-cards #card-price-container,
        body.detail-compact #dashboard-status-cards .tile-kwh-badge,
        body.detail-compact #dashboard-status-cards .status-tile-meta,
        body.detail-compact .dashboard-consumer-badge .tile-kwh-badge,
        body.detail-compact .dashboard-consumer-badge .wallbox-car-badge,
        body.detail-compact .dashboard-consumer-badge [id$="-session-container"],
        body.detail-compact .dashboard-consumer-badge #wp-morning-boost,
        body.detail-compact .dashboard-consumer-badge #wp-sg-ready-badge,
        body.detail-compact .dashboard-consumer-badge #wp-season-badge,
        body.detail-compact .dashboard-consumer-badge #wp-status-badge,
        body.detail-compact .dashboard-consumer-badge #hs-status-badge,
        body.detail-compact .dashboard-consumer-badge:not(.dashboard-wallbox-badge) .d-flex.flex-column.align-items-end {
            display: none !important;
        }
        body.detail-compact .dashboard-consumer-badge {
            align-self: stretch;
            width: 100%;
        }
        body.detail-compact .dashboard-wallbox-badge .wallbox-meta-stack {
            min-width: 2rem;
            max-width: 2rem;
        }
        body.detail-compact .dashboard-wallbox-badge .wallbox-meta-stack > :not(.wallbox-pause-btn) {
            display: none !important;
        }
	        body.detail-compact .dashboard-wallbox-badge .wallbox-pause-btn {
	            width: 1.85rem !important;
	            height: 1.85rem !important;
	            padding: 0 !important;
	            flex-shrink: 0;
	        }
	        body.frontend-modern .container-fluid {
	            max-width: 1840px;
	        }
	        body.frontend-modern .dashboard-view {
	            --modern-gap: 0.72rem;
	            display: grid;
	            grid-template-columns: minmax(0, 1fr) clamp(21rem, 24vw, 28rem);
	            grid-template-areas:
	                "alerts alerts"
	                "status side"
	                "main side";
	            align-items: start;
	            gap: var(--modern-gap);
	        }
	        body.frontend-modern .dashboard-view > .alert,
	        body.frontend-modern .dashboard-view > #wb-native-alert {
	            grid-column: 1 / -1;
	        }
	        body.frontend-modern #dashboard-status-cards-home {
	            grid-area: status;
	            min-width: 0;
	        }
	        body.frontend-modern #dashboard-main-layout {
	            display: contents;
	        }
	        body.frontend-modern #dashboard-chart-column {
	            grid-area: main;
	            width: 100%;
	            max-width: none;
	            min-width: 0;
	        }
	        body.frontend-modern #dashboard-side-column {
	            grid-area: side;
	            width: 100%;
	            max-width: none;
	            min-width: 0;
	        }
	        body.frontend-modern #dashboard-status-cards {
	            display: grid;
	            grid-template-columns: repeat(4, minmax(0, 1fr));
	            gap: var(--modern-gap);
	            margin: 0 !important;
	        }
	        body.frontend-modern #dashboard-status-cards > [class*="col-"] {
	            width: 100%;
	            max-width: none;
	            padding: 0 !important;
	        }
	        body.frontend-modern .dashboard-view .card {
	            border-radius: 8px;
	            box-shadow: none;
	            border-color: rgba(148, 163, 184, 0.22);
	        }
	        body.frontend-modern .dashboard-view .card:hover {
	            transform: none;
	        }
	        [data-bs-theme="dark"] body.frontend-modern .dashboard-view .card {
	            background-color: rgba(30, 30, 30, 0.76);
	        }
	        [data-bs-theme="light"] body.frontend-modern .dashboard-view .card {
	            background-color: rgba(255, 255, 255, 0.86);
	        }
	        body.frontend-modern #dashboard-status-cards .card-body {
	            min-height: 5.2rem;
	            padding: 0.72rem 0.82rem !important;
	        }
	        body.frontend-modern #dashboard-status-cards .icon-box {
	            width: 2.7rem;
	            height: 2.7rem;
	            min-width: 2.7rem;
	            border-radius: 999px;
	            font-size: 1.25rem;
	            margin-right: 0.72rem !important;
	        }
	        body.frontend-modern #dashboard-status-cards .val-large {
	            font-size: 1.45rem;
	            letter-spacing: 0;
	        }
	        body.frontend-modern #dashboard-status-cards .val-unit {
	            font-size: 0.78rem;
	        }
	        body.frontend-modern.detail-normal #dashboard-status-cards .tile-detail,
	        body.frontend-modern.detail-normal #dashboard-status-cards .status-tile-meta,
	        body.frontend-modern.detail-normal #dashboard-status-cards #card-price-container,
	        body.frontend-modern.detail-normal #dashboard-status-cards #val-eco-container {
	            display: none !important;
	        }
	        body.frontend-modern.detail-normal #dashboard-status-cards .tile-kwh-badge,
	        body.frontend-modern.detail-normal #dashboard-status-cards .status-tile-meta .tile-kwh-badge {
	            font-size: 0.66rem;
	        }
	        body.frontend-modern.detail-normal #dashboard-status-cards .card-body > .flex-grow-1.d-flex {
	            flex-direction: column;
	            align-items: center !important;
	            justify-content: center !important;
	            gap: 0.28rem;
	            min-width: 0;
	            text-align: center;
	        }
	        body.frontend-modern.detail-normal #dashboard-status-cards .card-body > .flex-grow-1.d-flex > .text-end {
	            flex-direction: row !important;
	            flex-wrap: wrap;
	            align-items: center !important;
	            justify-content: center !important;
	            align-self: stretch;
	            gap: 0.32rem !important;
	            margin-left: 0 !important;
	            margin-top: 0.08rem !important;
	            max-width: 100% !important;
	            padding-top: 0.12rem;
	            text-align: left !important;
	        }
	        body.frontend-modern.detail-normal #dashboard-status-cards .val-large {
	            font-size: 1.74rem !important;
	            line-height: 0.98 !important;
	        }
	        body.frontend-modern.detail-normal #dashboard-status-cards .val-unit {
	            font-size: 0.88rem !important;
	        }
	        body.frontend-modern.detail-normal #dashboard-status-cards .badge {
	            width: auto !important;
	            min-width: 0;
	            background: transparent !important;
	            border: 0 !important;
	            border-bottom: 0 !important;
	            border-radius: 0 !important;
	            box-shadow: none !important;
	            color: var(--bs-secondary-color) !important;
	            padding: 0 0.08rem !important;
	            text-align: left !important;
	        }
	        body.frontend-modern.detail-normal #dashboard-status-cards .home-climate-badge {
	            display: none !important;
	        }
	        body.frontend-modern.detail-normal #dashboard-status-cards .grid-kwh-meta.status-tile-meta {
	            display: flex !important;
	        }
	        body.frontend-modern:not(.detail-compact) #dashboard-status-cards {
	            padding: 0.1rem 0 0.35rem;
	            border-bottom: 1px solid rgba(148, 163, 184, 0.16);
	        }
	        body.frontend-modern:not(.detail-compact) #dashboard-status-cards .card {
	            background: transparent !important;
	            border: 0 !important;
	            border-radius: 0 !important;
	            box-shadow: none !important;
	        }
	        body.frontend-modern:not(.detail-compact) #dashboard-status-cards .card-body {
	            min-height: 4.7rem;
	            padding: 0.48rem 0.95rem !important;
	        }
	        body.frontend-modern:not(.detail-compact) #dashboard-status-cards > [class*="col-"]:not(:first-child) .card-body {
	            border-left: 1px solid rgba(148, 163, 184, 0.18);
	        }
	        body.frontend-modern:not(.detail-compact) #dashboard-status-cards .icon-box {
	            width: 2.15rem;
	            height: 2.15rem;
	            min-width: 2.15rem;
	            background: transparent !important;
	            border-radius: 0;
	            font-size: 1.08rem;
	            margin-right: 0.68rem !important;
	        }
	        body.frontend-modern:not(.detail-compact) #dashboard-status-cards .val-large {
	            font-size: 1.28rem;
	        }
		        body.frontend-modern:not(.detail-compact) #dashboard-status-cards .badge {
		            border-radius: 6px;
		            box-shadow: none !important;
		        }
		        body.frontend-modern.detail-detail #dashboard-status-cards .badge,
		        body.frontend-modern.detail-detail #right-column-cards .dashboard-consumer-badge .badge {
		            background: transparent !important;
		            border: 0 !important;
		            border-radius: 0 !important;
		            box-shadow: none !important;
		            color: var(--bs-secondary-color) !important;
		            padding: 0 !important;
		        }
		        body.frontend-modern.detail-detail #dashboard-status-cards .text-end,
		        body.frontend-modern.detail-detail #right-column-cards .dashboard-consumer-badge .d-flex.flex-column.align-items-end {
		            gap: 0.18rem !important;
		        }
		        body.frontend-modern.detail-detail #dashboard-status-cards .tile-kwh-badge,
		        body.frontend-modern.detail-detail #dashboard-status-cards .status-tile-meta,
		        body.frontend-modern.detail-detail #right-column-cards .dashboard-consumer-badge .tile-kwh-badge,
		        body.frontend-modern.detail-detail #right-column-cards .dashboard-consumer-badge [id$="-session-container"] .badge {
		            font-size: 0.68rem;
		            line-height: 1.18;
		        }
		        body.frontend-modern.detail-detail #dashboard-status-cards .tile-detail,
		        body.frontend-modern.detail-detail #right-column-cards .dashboard-consumer-badge .tile-detail {
		            color: var(--bs-secondary-color) !important;
		            font-size: 0.72rem !important;
		            line-height: 1.24;
		        }
		        body.frontend-modern.detail-detail #dashboard-status-cards .badge i,
		        body.frontend-modern.detail-detail #right-column-cards .dashboard-consumer-badge .badge i {
		            opacity: 0.9;
		        }
		        body.frontend-modern.detail-detail #dashboard-status-cards .battery-time-badge.is-placeholder {
		            display: none !important;
		        }
		        body.frontend-modern.detail-detail #dashboard-status-cards #val-eco-container {
		            display: none !important;
		        }
		        body.frontend-modern.detail-detail #dashboard-status-cards #grid-details {
		            max-width: 12rem;
		            white-space: normal !important;
		            font-size: 0.66rem !important;
		            line-height: 1.18 !important;
		            opacity: 0.72;
		        }
		        body.frontend-modern:not(.detail-compact) #right-column-cards .dashboard-consumer-badge .card {
		            background: transparent !important;
	            border: 0 !important;
	            border-radius: 0 !important;
	            border-bottom: 1px solid rgba(148, 163, 184, 0.16) !important;
	            box-shadow: none !important;
	        }
	        body.frontend-modern:not(.detail-compact) #right-column-cards .dashboard-consumer-badge .card-body {
	            min-height: 4.9rem !important;
	            padding: 0.68rem 0.2rem 0.7rem !important;
	        }
	        body.frontend-modern:not(.detail-compact) #right-column-cards .dashboard-consumer-badge .icon-box {
	            width: 2.25rem;
	            height: 2.25rem;
	            min-width: 2.25rem;
	            background: transparent !important;
	            border-radius: 0;
	            font-size: 1.05rem;
	        }
	        body.frontend-modern:not(.detail-compact) #right-column-cards .dashboard-consumer-badge .val-large {
	            font-size: 1.28rem;
	        }
	        body.frontend-modern:not(.detail-compact) #right-column-cards .dashboard-consumer-badge {
	            transition: opacity 0.18s ease, filter 0.18s ease;
	        }
	        body.frontend-modern:not(.detail-compact) #right-column-cards .dashboard-consumer-badge.modern-inactive {
	            opacity: 0.56;
	        }
	        body.frontend-modern.detail-detail #right-column-cards .dashboard-consumer-badge.modern-inactive {
	            opacity: 0.74;
	        }
	        body.frontend-modern.detail-normal #right-column-cards .dashboard-consumer-badge.modern-inactive .tile-kwh-badge,
	        body.frontend-modern.detail-normal #right-column-cards .dashboard-consumer-badge.modern-inactive .wallbox-car-badge,
	        body.frontend-modern.detail-normal #right-column-cards .dashboard-consumer-badge.modern-inactive [id$="-session-container"],
	        body.frontend-modern.detail-normal #right-column-cards .dashboard-consumer-badge.modern-inactive #wp-morning-boost,
	        body.frontend-modern.detail-normal #right-column-cards .dashboard-consumer-badge.modern-inactive #wp-sg-ready-badge,
	        body.frontend-modern.detail-normal #right-column-cards .dashboard-consumer-badge.modern-inactive #wp-season-badge,
	        body.frontend-modern.detail-normal #right-column-cards .dashboard-consumer-badge.modern-inactive #wp-status-badge,
	        body.frontend-modern.detail-normal #right-column-cards .dashboard-consumer-badge.modern-inactive #hs-status-badge,
	        body.frontend-modern.detail-normal #right-column-cards .dashboard-consumer-badge.modern-inactive #climate-card-link,
	        body.frontend-modern.detail-normal #right-column-cards .dashboard-consumer-badge.modern-inactive .tile-detail {
	            display: none !important;
	        }
	        body.frontend-modern.detail-normal #right-column-cards .dashboard-wallbox-badge.modern-inactive .wallbox-meta-stack {
	            min-width: 2.1rem;
	            max-width: 2.1rem;
	        }
	        body.frontend-modern.detail-normal #right-column-cards .dashboard-wallbox-badge.modern-inactive .wallbox-meta-stack > :not(.wallbox-pause-btn) {
	            display: none !important;
	        }
	        body.frontend-modern:not(.detail-compact) #right-column-cards .dashboard-consumer-badge .tile-kwh-badge,
	        body.frontend-modern:not(.detail-compact) #right-column-cards .dashboard-consumer-badge [id$="-session-container"] .badge,
	        body.frontend-modern:not(.detail-compact) #right-column-cards .dashboard-consumer-badge .wallbox-car-badge,
	        body.frontend-modern:not(.detail-compact) #right-column-cards .dashboard-consumer-badge #wp-season-badge,
	        body.frontend-modern:not(.detail-compact) #right-column-cards .dashboard-consumer-badge #wp-status-badge,
	        body.frontend-modern:not(.detail-compact) #right-column-cards .dashboard-consumer-badge #hs-status-badge,
	        body.frontend-modern:not(.detail-compact) #right-column-cards .dashboard-consumer-badge #climate-card-link {
	            background: transparent !important;
	            border: 0 !important;
	            border-left: 2px solid rgba(148, 163, 184, 0.28) !important;
	            border-radius: 0 !important;
	            box-shadow: none !important;
	            color: var(--bs-secondary-color) !important;
	            padding: 0 0 0 0.45rem !important;
	        }
	        body.frontend-modern:not(.detail-compact) #right-column-cards .dashboard-wallbox-badge.modern-active .wallbox-car-badge {
	            border-left-color: rgba(34, 197, 94, 0.55) !important;
	            color: #86efac !important;
	        }
	        body.frontend-modern:not(.detail-compact) #right-column-cards .dashboard-consumer-badge.modern-active #wp-status-badge,
	        body.frontend-modern:not(.detail-compact) #right-column-cards .dashboard-consumer-badge.modern-active #wp-season-badge,
	        body.frontend-modern:not(.detail-compact) #right-column-cards .dashboard-consumer-badge.modern-active #climate-card-link {
	            border-left-color: rgba(34, 211, 238, 0.55) !important;
	            color: #67e8f9 !important;
	        }
	        body.frontend-modern:not(.detail-compact) #right-column-cards .dashboard-consumer-badge.modern-offline {
	            opacity: 0.42;
	            filter: grayscale(0.3);
	        }
	        body.frontend-modern.detail-normal #right-column-cards .dashboard-consumer-badge .card-body {
	            min-height: 5.65rem !important;
	            padding-top: 0.62rem !important;
	            padding-bottom: 0.66rem !important;
	        }
	        body.frontend-modern.detail-normal #right-column-cards .dashboard-consumer-badge .card-body > .flex-grow-1 > .d-flex {
	            flex-direction: column;
	            align-items: center !important;
	            justify-content: center !important;
	            gap: 0.34rem;
	            text-align: center;
	        }
	        body.frontend-modern.detail-normal #right-column-cards .dashboard-consumer-badge .card-body > .flex-grow-1 > .d-flex > .d-flex.flex-column.align-items-end {
	            flex-direction: row !important;
	            flex-wrap: wrap;
	            align-items: center !important;
	            justify-content: center !important;
	            align-self: stretch;
	            gap: 0.36rem !important;
	            margin-left: 0 !important;
	            margin-top: 0.02rem !important;
	            max-width: 100% !important;
	            min-width: 0 !important;
	            padding-top: 0.12rem;
	            text-align: left !important;
	        }
	        body.frontend-modern.detail-normal #right-column-cards .dashboard-consumer-badge .val-large {
	            font-size: 1.92rem !important;
	            line-height: 0.98 !important;
	        }
	        body.frontend-modern.detail-normal #right-column-cards .dashboard-consumer-badge .val-unit {
	            font-size: 0.92rem !important;
	        }
	        body.frontend-modern.detail-normal #right-column-cards .dashboard-consumer-badge .badge {
	            width: auto !important;
	            min-width: 0;
	            max-width: 100%;
	            border-left: 0 !important;
	            border-bottom: 0 !important;
	            padding: 0 0.08rem !important;
	            text-align: left !important;
	        }
	        body.frontend-modern.detail-normal #right-column-cards .dashboard-wallbox-badge .card-body {
	            position: relative;
	            padding-right: 2.45rem !important;
	        }
	        body.frontend-modern.detail-normal #right-column-cards .dashboard-wallbox-badge .wallbox-pause-btn {
	            position: absolute;
	            top: 0.62rem;
	            right: 0.18rem;
	        }
	        body.frontend-modern.detail-detail #dashboard-status-cards .card-body > .flex-grow-1.d-flex {
	            flex-direction: column;
	            align-items: center !important;
	            justify-content: center !important;
	            gap: 0.28rem;
	            min-width: 0;
	            text-align: center;
	        }
	        body.frontend-modern.detail-detail #dashboard-status-cards .card-body > .flex-grow-1.d-flex > .text-end {
	            flex-direction: row !important;
	            flex-wrap: wrap;
	            align-items: center !important;
	            justify-content: center !important;
	            align-self: stretch;
	            gap: 0.34rem !important;
	            margin-left: 0 !important;
	            margin-top: 0.08rem !important;
	            max-width: 100% !important;
	            padding-top: 0.12rem;
	            text-align: center !important;
	        }
	        body.frontend-modern.detail-detail #dashboard-status-cards .val-large {
	            font-size: 1.74rem !important;
	            line-height: 0.98 !important;
	        }
	        body.frontend-modern.detail-detail #dashboard-status-cards .val-unit {
	            font-size: 0.88rem !important;
	        }
	        body.frontend-modern.detail-detail #dashboard-status-cards .badge {
	            width: auto !important;
	            min-width: 0;
	            border: 0 !important;
	            border-bottom: 0 !important;
	            padding: 0 0.08rem !important;
	            text-align: center !important;
	        }
	        body.frontend-modern.detail-detail #right-column-cards .dashboard-consumer-badge .card-body {
	            min-height: 6.35rem !important;
	            padding-top: 0.62rem !important;
	            padding-bottom: 0.66rem !important;
	        }
	        body.frontend-modern.detail-detail #right-column-cards .dashboard-consumer-badge .card-body > .flex-grow-1 > .d-flex {
	            flex-direction: column;
	            align-items: center !important;
	            justify-content: center !important;
	            gap: 0.34rem;
	            text-align: center;
	        }
	        body.frontend-modern.detail-detail #right-column-cards .dashboard-consumer-badge .card-body > .flex-grow-1 > .d-flex > :first-child {
	            width: 100%;
	            text-align: center;
	        }
	        body.frontend-modern.detail-detail #right-column-cards .dashboard-consumer-badge .card-body > .flex-grow-1 > .d-flex > :first-child > .d-flex {
	            justify-content: center;
	        }
	        body.frontend-modern.detail-detail #right-column-cards .dashboard-consumer-badge .card-body > .flex-grow-1 > .d-flex > .d-flex.flex-column.align-items-end {
	            flex-direction: row !important;
	            flex-wrap: wrap;
	            align-items: center !important;
	            justify-content: center !important;
	            align-self: stretch;
	            gap: 0.36rem !important;
	            margin-left: 0 !important;
	            margin-top: 0.02rem !important;
	            max-width: 100% !important;
	            min-width: 0 !important;
	            padding-top: 0.12rem;
	            text-align: center !important;
	        }
	        body.frontend-modern.detail-detail #right-column-cards .dashboard-consumer-badge .val-large {
	            font-size: 1.92rem !important;
	            line-height: 0.98 !important;
	        }
	        body.frontend-modern.detail-detail #right-column-cards .dashboard-consumer-badge .val-unit {
	            font-size: 0.92rem !important;
	        }
	        body.frontend-modern.detail-detail #right-column-cards .dashboard-consumer-badge .badge {
	            width: auto !important;
	            min-width: 0;
	            max-width: 100%;
	            border-left: 0 !important;
	            border-bottom: 0 !important;
	            padding: 0 0.08rem !important;
	            text-align: center !important;
	        }
	        body.frontend-modern.detail-detail #right-column-cards .dashboard-wallbox-badge .card-body {
	            position: relative;
	            padding-right: 2.45rem !important;
	        }
	        body.frontend-modern.detail-detail #right-column-cards .dashboard-wallbox-badge .wallbox-pause-btn {
	            position: absolute;
	            top: 0.62rem;
	            right: 0.18rem;
	        }
	        body.frontend-modern #dashboard-chart-column > .card {
	            min-height: min(720px, calc(100vh - 11.5rem));
	        }
	        body.frontend-modern #dashboard-chart-column > .card,
	        body.frontend-modern #card-regler-wrapper > .card,
	        body.frontend-modern #right-column-cards > .right-card-wrapper.mt-auto > .card {
	            background: transparent !important;
	            border-color: rgba(148, 163, 184, 0.20) !important;
	            box-shadow: none !important;
	        }
	        body.frontend-modern #dashboard-chart-column > .card > .card-header {
	            min-height: 3.1rem;
	            padding: 0.72rem 0.9rem !important;
	            border-bottom-color: rgba(148, 163, 184, 0.18) !important;
	            background: transparent !important;
	        }
	        body.frontend-modern #dashboard-chart-column #chart-title,
	        body.frontend-modern #card-regler-wrapper .card-title,
	        body.frontend-modern #right-column-cards > .right-card-wrapper.mt-auto .card-title {
	            letter-spacing: 0;
	            text-transform: none !important;
	        }
	        body.frontend-modern #dashboard-chart-column .chart-container {
	            background: transparent !important;
	        }
	        body.frontend-modern #liveChartContainer,
	        body.frontend-modern .flow-container {
	            background-color: transparent !important;
	        }
	        body.frontend-modern #dashboard-chart-column .chart-container {
	            height: clamp(500px, calc(100vh - 18rem), 700px);
	        }
	        body.frontend-modern #right-column-cards {
	            display: grid !important;
	            grid-auto-rows: auto;
	            gap: var(--modern-gap) !important;
	            height: auto !important;
	        }
	        body.frontend-modern #right-column-cards .right-card-wrapper {
	            margin-top: 0 !important;
	        }
	        body.frontend-modern #right-column-cards .card-body {
	            min-height: 5.35rem !important;
	            padding: 0.75rem 0.85rem !important;
	        }
	        body.frontend-modern #right-column-cards .icon-box {
	            width: 2.55rem;
	            height: 2.55rem;
	            min-width: 2.55rem;
	            border-radius: 999px;
	            font-size: 1.12rem;
	            margin-right: 0.72rem !important;
	        }
	        body.frontend-modern #right-column-cards .val-large {
	            font-size: 1.45rem;
	            letter-spacing: 0;
	        }
	        body.frontend-modern #right-column-cards .val-unit {
	            font-size: 0.76rem;
	        }
	        body.frontend-modern #right-column-cards .quick-action-btn {
	            border-radius: 999px;
	            padding: 0.48rem 0.65rem;
	            text-align: center !important;
	            white-space: nowrap;
	        }
	        body.frontend-modern #right-column-cards > .right-card-wrapper.mt-auto .card-body {
	            min-height: 0 !important;
	            padding: 0.9rem !important;
	        }
	        body.frontend-modern #right-column-cards > .right-card-wrapper.mt-auto h6 {
	            margin-bottom: 0.75rem !important;
	            color: var(--bs-secondary-color) !important;
	        }
	        body.frontend-modern #right-column-cards .quick-action-btn {
	            background: transparent !important;
	            color: var(--bs-secondary-color) !important;
	            border-color: rgba(148, 163, 184, 0.34) !important;
	        }
	        body.frontend-modern #right-column-cards .quick-action-btn.btn-info {
	            background: rgba(6, 182, 212, 0.16) !important;
	            color: #22d3ee !important;
	            border-color: rgba(34, 211, 238, 0.58) !important;
	        }
	        body.frontend-modern #card-regler-wrapper .card-body {
	            padding: 0.9rem 0.95rem !important;
	        }
	        body.frontend-modern #card-regler-wrapper .card-title {
	            border-bottom-color: rgba(34, 211, 238, 0.18) !important;
	            padding-bottom: 0.58rem !important;
	        }
	        body.frontend-modern #card-regler-wrapper .badge {
	            background: transparent !important;
	            border-color: rgba(34, 211, 238, 0.24) !important;
	        }
	        body.frontend-modern #card-regler-wrapper .card-body {
	            min-height: 0 !important;
	        }
	        .storage-curve-sparkline { position: relative; height: 48px; margin: 0 0 .55rem; border: 1px solid rgba(34,211,238,.16); border-radius: 8px; background: rgba(34,211,238,.035); overflow: hidden; }
	        .storage-curve-sparkline svg { display: block; width: 100%; height: 100%; }
	        .storage-curve-sparkline .sparkline-grid { fill: none; stroke: rgba(148,163,184,.18); stroke-width: 1; }
	        .storage-curve-sparkline .sparkline-forecast { fill: none; stroke: #22c55e; stroke-width: 2.4; stroke-linecap: round; stroke-linejoin: round; }
	        .storage-curve-sparkline .sparkline-target { fill: none; stroke: #22d3ee; stroke-width: 1.25; stroke-dasharray: 4 3; opacity: .72; stroke-linecap: round; stroke-linejoin: round; }
	        .storage-curve-sparkline-state { position: absolute; right: 6px; bottom: 3px; padding: 1px 5px; border-radius: 999px; background: rgba(15,23,42,.72); color: #94a3b8; font-size: .58rem; }
	        .storage-curve-sparkline[data-state="stale"] .sparkline-forecast { stroke: #f59e0b; stroke-dasharray: 4 3; }
	        .storage-curve-sparkline[data-state="missing"] polyline,
	        .storage-curve-sparkline[data-state="mismatch"] polyline,
	        .storage-curve-sparkline[data-state="stale"] polyline { display: none; }
	        body.frontend-modern.detail-compact .storage-curve-sparkline { display: none; }
	        body.frontend-modern.detail-compact #dashboard-status-cards-home {
	            display: none !important;
	        }
	        body.frontend-modern.detail-compact #dashboard-side-column {
	            padding-top: 0.45rem;
	        }
	        body.frontend-modern.detail-compact #dashboard-chart-column > .card {
	            min-height: min(760px, calc(100vh - 7.5rem));
	        }
	        body.frontend-modern.detail-compact #dashboard-chart-column .chart-container {
	            height: clamp(560px, calc(100vh - 14rem), 760px);
	        }
	        body.frontend-modern.detail-compact .dashboard-compact-badge-stack {
	            --compact-badge-gap: 0.55rem;
	            --compact-badge-height: 2.9rem;
	            margin-bottom: 0;
	        }
	        body.frontend-modern.detail-compact .dashboard-compact-badge-stack #dashboard-status-cards {
	            grid-template-columns: minmax(0, 1fr);
	            grid-auto-flow: row;
	            width: 100%;
	        }
	        body.frontend-modern.detail-compact .dashboard-compact-badge-stack #dashboard-status-cards > [class*="col-"] {
	            min-width: 0;
	        }
	        body.frontend-modern.detail-compact #dashboard-status-cards .card,
	        body.frontend-modern.detail-compact .dashboard-consumer-badge .card {
	            overflow: hidden;
	            border-color: rgba(148, 163, 184, 0.28);
	        }
	        body.frontend-modern.detail-compact #right-compact-badges {
	            padding: 0.15rem 0 0.35rem;
	            gap: 1rem;
	        }
		        body.frontend-modern.detail-compact #right-compact-badges .dashboard-compact-badge-column {
		            gap: 0;
		        }
		        body.frontend-modern.detail-compact #right-compact-main-badges {
		            display: block;
		        }
		        body.frontend-modern.detail-compact #right-compact-badges #dashboard-status-cards {
		            gap: 0;
		        }
	        body.frontend-modern.detail-compact #right-compact-badges .card {
	            background: transparent !important;
	            border: 0 !important;
	            border-radius: 0 !important;
	            box-shadow: none !important;
	        }
	        body.frontend-modern.detail-compact #right-compact-badges .card-body {
	            min-height: 0 !important;
	            height: var(--compact-badge-height);
	            padding: 0.22rem 0.2rem !important;
	            border-bottom: 1px solid rgba(148, 163, 184, 0.16);
	        }
	        body.frontend-modern.detail-compact #right-compact-main-badges .card-body {
	            border-left: 1px solid rgba(148, 163, 184, 0.22);
	            padding-left: 0.72rem !important;
	        }
	        body.frontend-modern.detail-compact #right-compact-consumer-badges .card-body {
	            border-left: 1px solid rgba(148, 163, 184, 0.16);
	            padding-left: 0.72rem !important;
	        }
	        body.frontend-modern.detail-compact #right-compact-badges .icon-box {
	            width: 1.55rem;
	            height: 1.55rem;
	            min-width: 1.55rem;
	            background: transparent !important;
	            border-radius: 0;
	            font-size: 0.95rem;
	            margin-right: 0.55rem !important;
	        }
	        body.frontend-modern.detail-compact #right-compact-badges .val-large {
	            font-size: 0.98rem;
	            line-height: 1;
	        }
	        body.frontend-modern.detail-compact #right-compact-badges .val-unit {
	            font-size: 0.66rem;
	            opacity: 0.72;
	        }
	        body.frontend-modern.detail-compact #right-compact-badges #card-price-container > .badge,
	        body.frontend-modern.detail-compact #right-compact-badges #val-eco-container {
	            background: transparent !important;
	            border: 0 !important;
	            padding: 0 !important;
	            color: var(--bs-secondary-color) !important;
	            box-shadow: none !important;
	        }
	        body.frontend-modern.detail-compact #right-compact-badges #val-price {
	            font-size: 0.9rem !important;
	        }
	        body.frontend-modern.detail-compact #dashboard-status-cards .card-body,
	        body.frontend-modern.detail-compact .dashboard-consumer-badge .card-body {
	            align-items: center;
	        }
	        body.frontend-modern.detail-compact #dashboard-status-cards .icon-box,
	        body.frontend-modern.detail-compact .dashboard-consumer-badge .icon-box {
	            margin-left: 0 !important;
	        }
	        body.frontend-modern.detail-compact #dashboard-status-cards .val-large,
	        body.frontend-modern.detail-compact .dashboard-consumer-badge .val-large {
	            font-size: 1rem;
	        }
	        @media (max-width: 1320px) {
	            body.frontend-modern .dashboard-view {
	                grid-template-columns: minmax(0, 1fr);
	                grid-template-areas:
	                    "alerts"
	                    "status"
	                    "main"
	                    "side";
	            }
	            body.frontend-modern #dashboard-status-cards {
	                grid-template-columns: repeat(2, minmax(0, 1fr));
	            }
	            body.frontend-modern #right-column-cards {
	                grid-template-columns: repeat(2, minmax(0, 1fr));
	            }
	            body.frontend-modern #right-compact-badges,
	            body.frontend-modern #card-regler-wrapper,
	            body.frontend-modern #right-column-cards .right-card-wrapper:last-child {
	                grid-column: 1 / -1;
	            }
	        }
	        @media (max-width: 720px) {
	            body.frontend-modern .container-fluid {
	                padding-left: 0.75rem !important;
	                padding-right: 0.75rem !important;
	            }
	            body.frontend-modern #dashboard-status-cards,
	            body.frontend-modern #right-column-cards {
	                grid-template-columns: minmax(0, 1fr);
	            }
	            body.frontend-modern #dashboard-chart-column .card-header {
	                align-items: stretch !important;
	                flex-direction: column;
	                gap: 0.65rem;
	            }
	            body.frontend-modern #dashboard-chart-column .card-header > div:last-child {
	                flex-wrap: wrap;
	            }
	            body.frontend-modern #dashboard-chart-column .chart-container {
	                height: 470px;
	            }
	        }

	        /* Anpassungen für inkludierte Seiten */
	        .container-fluid { max-width: 1920px; }
        /* --- Energiefluss Ansicht --- */
    .flow-container { background-color: #1e1e1e; color: #fff; border-radius: 0 0 12px 12px; position: relative; display: flex; flex-direction: column; width: 100%; height: 100%; overflow: hidden; font-family: sans-serif; }
    .flow-canvas { position: relative; flex: 1 1 auto; min-width: 0; min-height: 0; width: 100%; overflow: hidden; }
    .flow-svg { position: absolute; top: 0; left: 0; width: 100%; height: 100%; z-index: 1; pointer-events: none; }
    .flow-line { fill: none; stroke-width: 4; opacity: 0.2; }
    .flow-dots { fill: none; stroke-width: 6; stroke-dasharray: 0 20; animation: flowAnim 1s linear infinite; stroke-linecap: round; }
    @keyframes flowAnim { to { stroke-dashoffset: -60; } }
    .flow-dots.reverse { animation-direction: reverse; }
    .flow-dots.stopped { animation-play-state: paused; opacity: 0; }
    .flow-editor-toolbar { position: relative; z-index: 40; flex: 0 0 auto; display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 6px; width: 100%; min-height: 47px; padding: 7px 10px; border-bottom: 1px solid rgba(148, 163, 184, 0.22); }
    .flow-save-status { flex: 1 1 auto; align-self: center; min-width: 0; color: var(--bs-secondary-color, #94a3b8); font-size: 0.76rem; font-weight: 700; }
    .flow-save-status.is-success { color: #22c55e; }
    .flow-save-status.is-error { color: #ef4444; }
    .flow-editor-controls { display: none; flex: 1 1 430px; min-width: 0; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 6px; padding: 0; }
    .flow-container.flow-editing .flow-editor-controls { display: flex; }
    .flow-container.flow-editing .flow-node[data-flow-node] { cursor: grab; outline: 2px dashed rgba(255,255,255,0.42); outline-offset: 4px; }
    .flow-container.flow-editing .flow-node.flow-selected { outline-style: solid; outline-color: #fff; }
    .flow-container.flow-editing .external-wr-lock-btn { pointer-events: none; }
    .flow-container.flow-saving .flow-canvas { pointer-events: none; }
    .flow-color-select { width: auto; min-width: 112px; }
    .flow-color-input { width: 34px; height: 31px; padding: 2px; }
    .flow-label-input { width: 150px; }
    .flow-drag-handle { display: none; position: absolute; right: -12px; top: -12px; width: 30px; height: 30px; padding: 0; align-items: center; justify-content: center; border: 1px solid #94a3b8; border-radius: 50%; background: #111827; color: #f8fafc; box-shadow: 0 4px 12px rgba(0,0,0,.35); touch-action: none; user-select: none; z-index: 8; }
    .flow-container.flow-editing .flow-drag-handle { display: inline-flex; }
    .flow-node .flow-secondary-label { opacity: .78; font-size: .58rem; }
    .flow-node.node-aggregate { width: 108px; height: 108px; border-style: double; }
    .flow-node.node-aggregate .fa-icon { font-size: 1.7rem; }
    .flow-node.node-aggregate .val { font-size: 1.05rem; }

    .flow-node { position: absolute; transform: translate(-50%, -50%); border-radius: 50%; display: flex; flex-direction: column; align-items: center; justify-content: center; z-index: 10; box-sizing: border-box; padding: clamp(3px, 0.7vw, 9px); background: #1e1e1e; border: 4px solid; width: 130px; height: 130px; text-align: center; line-height: 1.03; }
    .flow-node.flow-dragging { transition: none !important; }
    .flow-node > .fa-icon { flex: 0 0 auto; max-width: calc(100% - 12px); font-size: clamp(1.35rem, 2.2vw, 2.2rem); margin-bottom: 4px; }
    .flow-node > .val { display: block; width: calc(100% - 8px); overflow: hidden; white-space: nowrap; font-size: clamp(0.78rem, 1.35vw, 1.3rem); line-height: 1.05; font-weight: bold; font-variant-numeric: tabular-nums; }
    .flow-node > .label { display: block; width: calc(100% - 10px); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: clamp(0.52rem, 0.75vw, 0.7rem); line-height: 1.05; color: #aaa; text-transform: uppercase; }
    .flow-node .flow-pv-split { max-width: 112px; margin-top: 1px; font-size: 0.58rem; line-height: 1.05; color: inherit; opacity: 0.82; text-transform: none; overflow: hidden; text-overflow: ellipsis; }
    .flow-zero-export-badge { position: absolute; top: -13px; right: -38px; display: inline-flex; align-items: center; justify-content: center; gap: 4px; min-width: 78px; height: 23px; padding: 0 7px; border: 1px solid transparent; border-radius: 999px; color: #fff; font-size: 0.61rem; font-weight: 800; line-height: 1; letter-spacing: 0; white-space: nowrap; z-index: 3; box-shadow: 0 4px 12px rgba(15,23,42,0.28); cursor: help; }
    .flow-zero-export-badge[hidden] { display: none !important; }
    .flow-zero-export-badge.is-confirmed { background: #15803d; border-color: #4ade80; }
    .flow-zero-export-badge.is-settling { background: #a16207; border-color: #facc15; }
    .flow-zero-export-badge.is-violation { background: #b91c1c; border-color: #f87171; }
    .flow-container.flow-editing .flow-zero-export-badge { pointer-events: none; }
    .flow-node.node-external-pv { width: 104px; height: 104px; border-width: 3px; }
    .flow-node.node-external-pv .fa-icon { font-size: 1.65rem; margin-bottom: 2px; }
    .flow-node.node-external-pv .val { font-size: 1rem; }
    .flow-node.node-external-pv .label { max-width: 78px; font-size: 0.55rem; line-height: 1.05; }
    .flow-node.node-external-pv.is-producing { animation: externalWrPulse 2.4s ease-in-out infinite; }
    .flow-node.node-external-pv.is-manual-locked { border-color: #dc3545 !important; color: #dc3545 !important; box-shadow: 0 0 20px rgba(220,53,69,0.42) !important; }
    .flow-node.node-external-pv.is-price-locked { border-color: #f59e0b !important; color: #f59e0b !important; box-shadow: 0 0 18px rgba(245,158,11,0.34) !important; }
    @keyframes externalWrPulse { 0%, 100% { box-shadow: 0 0 14px rgba(34,197,94,0.28); } 50% { box-shadow: 0 0 26px rgba(34,197,94,0.62); } }
    #f-external-pv-lock { position: absolute; top: 12%; right: 18%; font-size: 0.68rem; }
    .external-wr-lock-btn { position: absolute; right: -8px; bottom: -8px; display: inline-flex; align-items: center; justify-content: center; width: 29px; height: 29px; padding: 0; border: 1px solid rgba(148,163,184,0.55); border-radius: 50%; background: var(--bs-body-bg); color: var(--bs-body-color); z-index: 2; }
    .external-wr-lock-btn:hover, .external-wr-lock-btn:focus-visible { border-color: #dc3545; color: #dc3545; }
    .external-wr-lock-btn[aria-pressed="true"] { background: #dc3545; border-color: #dc3545; color: #fff; }
    .flow-node .price-tag { position: absolute; bottom: -20px; background: #222; padding: 2px 6px; border-radius: 8px; font-size: 0.7rem; font-weight: bold; border: 1px solid #444; white-space: nowrap; }
    .flow-hover-panel { position: absolute; z-index: 45; display: none; min-width: 230px; max-width: 285px; padding: 10px 12px; border-radius: 8px; border: 1px solid rgba(148, 163, 184, 0.32); background: rgba(15, 23, 42, 0.94); color: #f8fafc; box-shadow: 0 14px 34px rgba(0,0,0,0.32); pointer-events: none; text-align: left; font-size: 0.78rem; line-height: 1.25; backdrop-filter: blur(12px); }
    .flow-hover-panel.is-visible { display: block; }
    .flow-hover-title { display: flex; justify-content: space-between; gap: 10px; align-items: baseline; font-weight: 800; margin-bottom: 6px; }
    .flow-hover-now { color: #38bdf8; white-space: nowrap; }
    .flow-hover-meta { display: flex; justify-content: space-between; gap: 8px; color: rgba(226,232,240,0.78); margin-bottom: 7px; }
    .flow-hover-bar { display: flex; overflow: hidden; height: 10px; border-radius: 5px; background: rgba(148,163,184,0.18); margin-bottom: 7px; }
    .flow-hover-seg { min-width: 3px; height: 100%; }
    .flow-hover-row { display: flex; justify-content: space-between; gap: 10px; align-items: center; margin-top: 4px; }
    .flow-hover-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 5px; }
    .flow-hover-note { margin-top: 6px; color: rgba(226,232,240,0.68); }

    /* Backing Circle für Batterie */
    .flow-node-back {
        position: absolute; transform: translate(-50%, -50%); border-radius: 50%;
        z-index: 5; background-color: #1e1e1e;
        width: 130px; height: 130px;
        top: 15%; left: 50%;
    }

    /* Node Farben / Glow */
    .node-pv { border-color: #ffc107; color: #ffc107; box-shadow: 0 0 20px rgba(255,193,7,0.4); top: 25%; left: 20%; }
    .node-grid { border-color: #6c757d; color: #ced4da; box-shadow: 0 0 20px rgba(108,117,125,0.4); top: 75%; left: 20%; }
    .node-bat { border-color: #dc3545; color: #dc3545; box-shadow: 0 0 20px rgba(220,53,69,0.4); top: 16%; left: 50%; }
    .node-bat.charging { border-color: #2ecc71; color: #2ecc71; box-shadow: 0 0 20px rgba(46,204,113,0.4); }
    .node-home { border-color: #0dcaf0; color: #0dcaf0; box-shadow: 0 0 20px rgba(13,202,240,0.4); top: 32%; left: 72%; }
    .node-wb { border-color: #2ecc71; color: #2ecc71; box-shadow: 0 0 20px rgba(46,204,113,0.4); top: 68%; left: 72%; }
    .node-wp { border-color: #f97316; color: #f97316; box-shadow: 0 0 20px rgba(249,115,22,0.38); top: 50%; left: 72%; }
    .node-hs { border-color: #fd7e14; color: #fd7e14; box-shadow: 0 0 20px rgba(253,126,20,0.4); }
    .node-climate { border-color: #38bdf8; color: #38bdf8; box-shadow: 0 0 20px rgba(56,189,248,0.38); }
    .node-wp.boost { border-color: #dc3545; color: #dc3545; box-shadow: 0 0 25px rgba(220,53,69,0.8); }
    .node-center { background: transparent; border: none; box-shadow: none; width: 90px; height: 90px; top: 50%; left: 50%; padding: 0; }
    .node-center img { width: 100%; height: 100%; border-radius: 50%; box-shadow: 0 0 30px #0d6efd; }

    @media (max-width: 900px) {
        .flow-node, .flow-node-back { transform: translate(-50%, -50%) scale(0.60); }
        .node-pv { left: 20%; }
        .node-home { left: 76%; top: 24%; }
        .node-wb { left: 76%; top: 72%; }
        .node-wp { left: 76%; top: 48%; }
        .flow-container { min-height: 450px; }
        .flow-container.flow-has-wb2 { min-height: 560px; }
        .flow-container.flow-has-wb2.flow-has-hs { min-height: 590px; }
        .flow-container.flow-has-consumption-aggregate { min-height: 590px; }
        .flow-editor-toolbar { align-items: flex-start; }
        .flow-editor-controls { flex-basis: 100%; max-width: 100%; overflow-x: auto; }
    }

    /* Light Mode Flow Overrides */
    [data-bs-theme="light"] .flow-container { background-color: #f8fafc; color: #334155; }
    [data-bs-theme="light"] .flow-node { background-color: #ffffff; border-color: #cbd5e1; }
    [data-bs-theme="light"] .flow-node .label { color: #6c757d; }
    [data-bs-theme="light"] .flow-node .price-tag { background-color: #ffffff; border-color: #cbd5e1; }
    [data-bs-theme="light"] .flow-node-back { background-color: #ffffff; }
    [data-bs-theme="light"] .flow-editor-controls { background: rgba(255,255,255,0.88); border-color: rgba(100,116,139,0.28); }
    [data-bs-theme="light"] .flow-hover-panel { background: rgba(255,255,255,0.96); color: #0f172a; border-color: rgba(100,116,139,0.28); box-shadow: 0 14px 34px rgba(15,23,42,0.18); }
    [data-bs-theme="light"] .flow-hover-meta { color: #64748b; }
    [data-bs-theme="light"] .flow-hover-note { color: #64748b; }

    /* Batterie SOC-Gradient: CSS-Variablen werden per JS gesetzt, --bs-body-bg reagiert sofort auf Theme-Wechsel */
    #f-node-bat {
        background: linear-gradient(to top,
            var(--bat-fill, transparent) var(--bat-soc, 0%),
            var(--bs-body-bg) var(--bat-soc-top, 10%)
        ) !important;
    }

        /* Sticky Header mit Frosted-Glass Effekt */
        .navbar {
            position: sticky;
            top: 0;
            z-index: 1030;
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            transition: box-shadow 0.3s ease, background-color 0.3s ease;
        }
        [data-bs-theme="dark"] .navbar {
            background-color: rgba(18, 18, 18, 0.82) !important;
            border-bottom-color: rgba(255,255,255,0.08) !important;
        }
        [data-bs-theme="light"] .navbar {
            background-color: rgba(238, 242, 246, 0.82) !important;
            border-bottom-color: rgba(0,0,0,0.08) !important;
        }
        .navbar.scrolled {
            box-shadow: 0 4px 24px rgba(0,0,0,0.18);
        }
        .e3dc-topbar .container-fluid {
            gap: 0.75rem;
            min-width: 0;
        }
        .e3dc-topbar .navbar-brand {
            flex: 0 0 auto;
            margin-right: 0.25rem;
            white-space: nowrap;
        }
        .e3dc-topbar .navbar-brand.e3dc-app-brand {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 2.1rem;
            min-width: 2.1rem;
            height: 2.1rem;
            padding: 0;
            margin-right: 0.1rem;
            border-radius: 8px;
            overflow: hidden;
        }
        .e3dc-app-brand-icon {
            width: 1.85rem;
            height: 1.85rem;
            display: block;
            border-radius: 6px;
        }
        #header-live-values.e3dc-header-live {
            flex: 1 1 34rem;
            min-width: 0;
            max-width: 42rem;
            overflow: hidden;
            justify-content: center;
        }
        #header-live-values.e3dc-header-live > div {
            flex: 0 0 auto;
            white-space: nowrap;
        }
        .e3dc-header-actions {
            flex: 0 0 auto;
            min-width: 0;
        }
        .e3dc-header-actions > a,
        .e3dc-header-actions > button.btn-link {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 1.45rem;
            min-width: 1.45rem;
            height: 1.75rem;
            text-decoration: none;
        }
        #clock-wrapper {
            flex: 0 0 auto;
        }
        #head-out-temp {
            white-space: nowrap;
        }
        @media (max-width: 1850px) {
            .e3dc-topbar .container-fluid {
                padding-left: 0.85rem !important;
                padding-right: 0.85rem !important;
            }
            #header-live-values.e3dc-header-live {
                gap: 0.55rem !important;
                padding: 0.2rem 0.7rem !important;
                font-size: 0.76rem;
            }
            .e3dc-header-actions {
                gap: 0.65rem !important;
            }
            #cpu-badge {
                display: none !important;
            }
            #weather-alert-badge {
                padding-left: 0.55em;
                padding-right: 0.55em;
            }
            #weather-alert-badge .fa-cloud-sun {
                margin-right: 0 !important;
            }
            #weather-alert-badge-text {
                display: none;
            }
        }
        @media (max-width: 1700px) {
            #head-eco-container {
                display: none !important;
            }
            #clock-wrapper {
                margin-left: 0.25rem !important;
                padding-left: 0.75rem !important;
            }
            #head-out-temp {
                margin-right: 0.65rem !important;
                font-size: 0.95rem !important;
            }
            #clock {
                font-size: 1rem !important;
            }
            #date {
                display: none;
            }
        }
        @media (max-width: 1550px) {
            #header-live-values.e3dc-header-live {
                display: none !important;
            }
        }

        body:not([data-chart-mode="flow"]) #header-regler-plan {
            display: none !important;
        }
    </style>
</head>
<?php
$confData = loadE3dcConfig();
$dashCfg = $confData['config'] ?? [];
$climateEnabled = cfgBool($dashCfg['climate_enable'] ?? '0', false);
$initialChartView = strtolower(trim((string)($_GET['view'] ?? '')));
?>
<body class="frontend-<?= htmlspecialchars($frontendVariant ?? 'classic', ENT_QUOTES) ?> detail-<?= htmlspecialchars($frontendDetailMode ?? 'normal', ENT_QUOTES) ?>" data-frontend="<?= htmlspecialchars($frontendVariant ?? 'classic', ENT_QUOTES) ?>" data-detail-mode="<?= htmlspecialchars($frontendDetailMode ?? 'normal', ENT_QUOTES) ?>">
    <nav class="navbar navbar-expand-lg bg-body-tertiary border-bottom border-secondary mb-3 py-1 e3dc-topbar">
        <div class="container-fluid px-3">
            <a class="navbar-brand e3dc-app-brand" href="index.php" title="Dashboard" aria-label="Dashboard">
                <img class="e3dc-app-brand-icon" src="<?= getAssetUrl('app-icon-192.png') ?>" alt="" width="30" height="30">
                <span class="visually-hidden">E3DC Control Dashboard</span>
            </a>
            <?php if ($seite !== 'dashboard'): ?>
            <div class="d-none d-lg-flex gap-2 mx-auto align-items-center bg-body-secondary px-3 py-1 rounded-pill border border-secondary-subtle shadow-sm small e3dc-header-live" id="header-live-values">
                <div title="PV Leistung"><i class="fas fa-sun text-warning"></i> <span id="head-pv" class="fw-bold">--</span></div>
                <div title="Hausakku"><i class="fas fa-battery-half text-muted" id="head-icon-bat"></i> <span id="head-bat" class="fw-bold">--</span> <span id="head-soc" class="text-muted" style="font-size:0.85em" title="Hausakku-SoC">--%</span></div>
                <div title="Hausverbrauch"><i class="fas fa-home text-info"></i> <span id="head-home" class="fw-bold">--</span></div>
                <div title="Netz"><i class="fas fa-network-wired text-secondary" id="head-icon-grid"></i> <span id="head-grid" class="fw-bold">--</span></div>
                <?php if($wbEnabled): ?>
                <div title="Wallbox"><i class="fas fa-charging-station text-secondary" id="head-icon-wb"></i> <span id="head-wb" class="fw-bold">--</span></div>
                <?php endif; ?>
                <div title="Strompreis"><i class="fas fa-tag text-secondary" id="head-icon-price"></i> <span id="head-price" class="fw-bold">--</span></div>
                <div title="Eco-Score (Intelligenz)" id="head-eco-container" style="display:none;"><i class="fas fa-brain text-success"></i> <span id="head-eco-score" class="fw-bold text-success">--</span></div>
                <?php if($wpEnabled): ?>
                <div title="Wärmepumpe"><i class="fas fa-fire text-danger" id="head-icon-wp"></i> <span id="head-wp" class="fw-bold">--</span></div>
                <?php endif; ?>
                <?php if($climateEnabled): ?>
                <div title="Klima"><i class="fas fa-snowflake text-info" id="head-icon-climate"></i> <span id="head-climate" class="fw-bold">--</span></div>
                <?php endif; ?>
            </div>
            <?php endif; ?>
            <div class="d-flex align-items-center gap-2 e3dc-header-actions">
                <?php if($wbEnabled): ?>
                <a href="index.php?seite=wallbox" title="Wallbox">
                    <i class="fas fa-charging-station fa-lg text-success"></i>
                </a>
                <?php endif; ?>
                <a href="index.php?seite=fahrzeug" title="Fahrzeug Info">
                    <i class="fas fa-car fa-lg text-info"></i>
                </a>
                <?php if($wpEnabled || $hsEnabled): ?>
                <a href="index.php?seite=waermepumpe" title="<?= $wpEnabled ? 'Wärmepumpe' : 'Heizstab' ?>">
                    <i class="fas fa-fire<?= $wpEnabled ? '' : '-burner' ?> fa-lg <?= $wpEnabled ? 'text-danger' : 'text-warning' ?>"></i>
                </a>
                <?php endif; ?>
                <?php if($climateEnabled): ?>
                <a href="index.php?seite=klima" title="Klima">
                    <i class="fas fa-snowflake fa-lg text-info"></i>
                </a>
                <?php endif; ?>
                <a href="index.php?seite=langzeit" title="Langzeit-Statistiken">
                    <i class="fas fa-calendar-alt fa-lg text-warning"></i>
                </a>
                <a href="index.php?seite=vitals" title="Vitalwerte & Batterie-Diagnose">
                    <i class="fas fa-heartbeat fa-lg text-danger"></i>
                </a>
                <button type="button" class="btn btn-link p-0" onclick="showGridHealthModal()" title="Grid Health">
                    <i class="fas fa-bolt fa-lg text-warning"></i>
                </button>
                <?php if (!empty($dashCfg['matter_bridge']) && $dashCfg['matter_bridge'] == '1'): ?>
                <a href="index.php?seite=matter" title="Matter Smart Home Bridge">
                    <i class="fas fa-atom fa-lg text-primary"></i>
                </a>
                <?php endif; ?>
                <a href="index.php?seite=config" class="position-relative" title="Konfiguration">
                    <i class="fas fa-cog fa-lg text-secondary"></i>
                </a>
                <?= renderConnectionBadge() ?>
                <span id="watchdog-badge" class="badge rounded-pill bg-secondary" style="display:none; cursor:pointer;" onclick="showWatchdogLog()" title="Watchdog Status">
                    <i class="fas fa-shield-alt"></i>
                </span>
                <span id="cpu-badge" class="badge bg-body-tertiary text-secondary border border-secondary-subtle" style="font-family:monospace; display:none; font-size: 0.75rem;" title="CPU Load (1min) / Temperatur">CPU: --</span>

                <button class="btn btn-link p-0" onclick="toggleDarkMode()" title="Dark Mode umschalten">
                    <i class="fas fa-<?= $darkMode ? 'sun' : 'moon' ?> text-warning" id="darkmode-icon"></i>
                </button>
                <button class="btn btn-link p-0 detail-toggle-button" id="tiledetails-button" onclick="toggleTileDetails()" title="Ansichtsdichte umschalten" aria-label="Ansichtsdichte umschalten">
                    <i class="fas fa-eye text-primary" id="tiledetails-icon"></i>
                </button>
                <a href="help.php" title="Hilfe & FAQ">
                    <i class="fas fa-question-circle fa-lg text-info"></i>
                </a>

        <?php if (!empty($dashCfg['web_pin'])): ?>
            <?php if (isWebAuthenticated()): ?>
                <form method="post" class="d-inline ms-1">
                    <?= e3dcCsrfInput() ?>
                    <input type="hidden" name="action" value="web_logout">
                    <button type="submit" class="btn btn-link border-0 p-0 align-baseline" title="Sperren (Logout)" aria-label="Sperren">
                        <i class="fas fa-unlock text-success fa-lg"></i>
                    </button>
                </form>
            <?php else: ?>
                <a href="?seite=lock" class="ms-1" title="Entsperren (Login)"><i class="fas fa-lock text-warning fa-lg"></i></a>
            <?php endif; ?>
        <?php endif; ?>

                <div class="d-flex align-items-center ms-2 ps-3 border-start border-secondary" id="clock-wrapper">
                    <span id="weather-alert-badge" class="badge rounded-pill bg-body-tertiary text-info border border-info-subtle me-2" style="display:none;" title="Wetter am Anlagenstandort" role="button" tabindex="0" aria-label="Wetterhinweis anzeigen">
                        <i class="fas fa-cloud-sun me-1"></i><span id="weather-alert-badge-text">Wetter</span>
                    </span>
                    <div id="head-out-temp" class="text-info fw-bold me-3" style="font-size: 1.15rem;" title="Außentemperatur">-- °C</div>
                    <div class="text-end">
                        <div id="clock" class="fw-bold text-body" style="font-size: 1.1rem; line-height: 1;">--:--</div>
                        <div class="small text-muted" id="date" style="line-height: 1.2;">--.--.----</div>
                    </div>
                </div>
            </div>
        </div>
    </nav>

    <div class="container-fluid px-4 pb-5">
        <?php if ($seite === 'dashboard'): ?>
        <div class="dashboard-view">

        <!-- Notstrom Alert -->
        <div id="notstrom-alert" class="alert alert-danger d-flex align-items-center mb-3 shadow pulsating" style="display:none !important;">
            <i class="fas fa-bolt-lightning fs-2 me-3"></i>
            <div>
                <h5 class="alert-heading fw-bold mb-1">STROMAUSFALL (Notstrom aktiv)</h5>
                <div class="small">Das E3DC-System hat das Hausnetz vom öffentlichen Stromnetz getrennt und versorgt das Haus aus der Batterie. Bitte große Verbraucher abschalten!</div>
            </div>
        </div>

        <!-- Watchdog Failsafe Alert -->
        <div id="watchdog-alert" class="alert alert-warning d-flex align-items-center mb-3 shadow pulsating" style="display:none !important; border-radius: 12px; background-color: #ffc107; color: #000;">
            <i class="fas fa-exclamation-triangle fs-2 me-3"></i>
            <div>
                <h5 class="alert-heading fw-bold mb-1">WATCHDOG FAILSAFE</h5>
                <div class="small" id="watchdog-alert-text">Ein Kerndienst ist ausgefallen! System arbeitet nativ.</div>
            </div>
        </div>

        <!-- Native Storage/Wallbox Dashboard Status -->
        <?php $storageStatusEnabled = (float)($batteryCapacity ?? 0) > 0.0; ?>
        <?php $heatManagerEnabled = !empty($wpEnabled) || !empty($hsEnabled); ?>
        <?php $nativeWallboxStatusEnabled = hasNativeWallboxStatusConfig($_c ?? []); ?>
        <?php if($storageStatusEnabled): ?>
        <div id="wb-native-alert" class="card shadow-sm mb-3 border-0" style="display:none; background: linear-gradient(135deg, rgba(168, 85, 247, 0.08) 0%, rgba(168, 85, 247, 0.01) 100%);">
            <div class="card-body p-2 px-3 d-flex align-items-stretch" style="min-height:72px; gap:0;">

                <!-- === Spalte 1: Storage Manager & Simulator (links, groesste Spalte) === -->
                <div class="d-flex flex-column justify-content-center pe-3 flex-grow-1" style="min-width:0; <?php if($nativeWallboxStatusEnabled || $heatManagerEnabled): ?>border-right:1px solid rgba(108,117,125,0.22);<?php endif; ?>">
                    <div class="text-uppercase fw-bold d-flex align-items-center gap-1 mb-1" style="font-size:0.6rem; letter-spacing:0.06em; color:#818cf8;">
                        <i class="fas fa-brain" style="font-size:0.62rem;"></i> Speicherregelung &amp; Prognose
                    </div>
                    <div class="d-flex align-items-center gap-2 flex-wrap">
                        <span class="fw-bold" style="font-size:1.05rem; line-height:1;" id="wb-stor-state">--</span>
                        <span class="badge rounded-pill" style="font-size:0.65rem; background:rgba(108,117,125,0.12); color:#adb5bd;" id="wb-budget-state-badge">--</span>
                        <span class="text-muted" style="font-size:0.78rem;" id="wb-stor-soll-soc">Soll: -- %</span>
                        <span class="badge rounded-pill" style="display:none; font-size:0.65rem; background:rgba(129,140,248,0.14); color:#a5b4fc;" id="wb-stor-ifc">Rahmen: -- W</span>
                        <span class="badge rounded-pill" style="display:none; font-size:0.65rem; background:rgba(14,165,233,0.12); color:#38bdf8;" id="wb-stor-curve">Kurve: --</span>
                        <span class="badge rounded-pill" style="font-size:0.65rem;" id="wb-budget-badge">Budget: -- W</span>
                        <span class="badge rounded-pill" style="display:none; font-size:0.65rem; background:rgba(245,158,11,0.15); color:#f59e0b;" id="storage-forecast-badge">--</span>
                    </div>
                    <div class="text-muted text-truncate mt-1" style="font-size:0.7rem;" id="wb-stor-reason" title="">--</div>
                    <div id="wb-stor-dv-status" class="mt-1 d-flex flex-wrap align-items-center gap-2" style="display:none; font-size:0.68rem;">
                        <span class="fw-bold text-success"><i class="fas fa-scale-balanced me-1"></i>DV</span>
                        <span id="wb-stor-dv-badge" class="badge rounded-pill bg-secondary bg-opacity-25 text-secondary">--</span>
                        <span id="wb-stor-dv-detail" class="text-muted text-truncate" style="min-width:0;" title="">--</span>
                    </div>
                </div>

                <!-- === Spalte 2: Wärme Regelung === -->
                <div id="heat-manager-col" data-heat-configured="<?= $heatManagerEnabled ? '1' : '0' ?>" class="<?= $heatManagerEnabled ? 'd-flex ' : '' ?>flex-column justify-content-center px-3" style="<?php if(!$heatManagerEnabled): ?>display:none !important; <?php endif; ?>min-width:190px; flex-shrink:0; <?php if($nativeWallboxStatusEnabled): ?>border-right:1px solid rgba(108,117,125,0.22);<?php endif; ?>">
                    <div class="text-uppercase fw-bold d-flex align-items-center gap-1 mb-1" style="font-size:0.6rem; letter-spacing:0.06em; color:#fb923c;">
                        <i class="fas fa-fire" style="font-size:0.62rem;"></i> Wärme Regelung
                    </div>
                    <div class="d-flex align-items-center gap-3">
                        <div>
                            <div class="fw-bold" style="font-size:1.05rem; color:#fb923c; line-height:1;" id="heat-manager-state">--</div>
                            <div class="text-muted" style="font-size:0.65rem;" id="heat-manager-state-label">Manager</div>
                        </div>
                        <div class="d-flex flex-column gap-1">
                            <span class="badge rounded-pill" style="font-size:0.65rem; background:rgba(108,117,125,0.12); color:#adb5bd;" id="heat-manager-mode-badge">--</span>
                            <span style="font-size:0.75rem;" id="heat-manager-wp-mode">--</span>
                        </div>
                    </div>
                    <div class="text-muted text-truncate mt-1" style="font-size:0.68rem;" id="heat-manager-budget" title="">Budget: --</div>
                </div>

                <!-- === Spalte 3: Wallbox Regelung (mitte) === -->
                <div id="wb-native-col2" data-wallbox-configured="<?= $nativeWallboxStatusEnabled ? '1' : '0' ?>" class="<?= $nativeWallboxStatusEnabled ? 'd-flex ' : 'd-none ' ?>flex-column justify-content-center px-3" <?php if(!$nativeWallboxStatusEnabled): ?>hidden aria-hidden="true"<?php else: ?>aria-hidden="false"<?php endif; ?> style="<?php if(!$nativeWallboxStatusEnabled): ?>display:none !important; <?php endif; ?>min-width:210px; flex-shrink:0; border-right:1px solid rgba(108,117,125,0.22);">
                    <div class="text-uppercase fw-bold d-flex align-items-center gap-1 mb-1" style="font-size:0.6rem; letter-spacing:0.06em; color:#a855f7;">
                        <i class="fas fa-sliders-h" style="font-size:0.62rem;"></i> Wallbox Regelung
                        <span class="badge rounded-pill bg-secondary bg-opacity-25 text-secondary ms-auto" id="wb-native-count" style="font-size:0.58rem;">-- WB</span>
                    </div>
                    <div class="d-flex align-items-center gap-3">
                        <div>
                            <div class="fw-bold" style="font-size:1.2rem; color:#a855f7; line-height:1;"><span id="wb-native-amp">0</span><small id="wb-native-amp-unit" style="font-size:0.62rem; margin-left:2px;">A</small></div>
                            <div class="text-muted" style="font-size:0.65rem;" id="wb-native-amp-label">Soll-Strom</div>
                        </div>
                        <div class="d-flex flex-column gap-1">
                            <span class="badge rounded-pill" style="font-size:0.65rem;" id="wb-mode-badge">Modus --</span>
                            <span style="font-size:0.75rem;" id="wb-native-batstate">Normal</span>
                        </div>
                        <div class="text-muted d-flex flex-column" style="font-size:0.68rem; line-height:1.7;">
                            <span>Fz: <span id="wb-fuzzy-factor" class="fw-bold text-body">--</span></span>
                            <span>Cap: <span id="wb-cap-amp" class="fw-bold text-body">--</span></span>
                            <span id="wb-native-kva-badge" style="display:none; color:#22d3ee; font-weight:600;" title="Scheinleistung: Spannung x Strom je Phase. Die Regelung nutzt weiterhin Wirkleistung in W.">-- kVA</span>
                        </div>
                    </div>
                </div>

                <!-- === Spalte 4: Verbindungsstatus + WB-Name + WB-Details (rechts) === -->
                <div id="wb-native-col3" data-wallbox-configured="<?= $nativeWallboxStatusEnabled ? '1' : '0' ?>" class="<?= $nativeWallboxStatusEnabled ? 'd-flex ' : 'd-none ' ?>flex-column justify-content-center ps-3" <?php if(!$nativeWallboxStatusEnabled): ?>hidden aria-hidden="true"<?php else: ?>aria-hidden="false"<?php endif; ?> style="<?php if(!$nativeWallboxStatusEnabled): ?>display:none !important; <?php endif; ?>min-width:290px; flex-shrink:0;">
                    <div class="d-flex align-items-center">
                        <div id="wb-native-pulse" class="rounded-circle me-2 flex-shrink-0" style="width:12px; height:12px; background:#a855f7;"></div>
                        <div style="min-width:0;">
                            <div class="fw-bold text-truncate" style="font-size:0.85rem; line-height:1.3;">
                                <span id="wb-native-status">Warte auf Daten...</span>
                            </div>
                            <div class="text-muted text-truncate" style="font-size:0.68rem; margin-top:1px;" id="wb-native-status-detail" title="">
                                <i class="fas fa-charging-station text-warning me-1" style="font-size:0.65rem;"></i><span id="wb-native-type">Wallbox</span>
                            </div>
                        </div>
                    </div>
                    <!-- Multi-WB Details (nur bei >1 WB) -->
                    <div id="wb-native-multi-details" class="d-none d-flex gap-2 mt-2">
                        <div id="wb-native-1-slot" class="flex-fill rounded-2 px-2 py-1 wb-native-slot" style="border:1px solid rgba(108,117,125,0.24); background:rgba(108,117,125,0.06); min-width:0;">
                            <div class="d-flex align-items-center gap-1 text-muted text-uppercase" style="font-size:0.55rem;">
                                <span id="wb-native-1-dot" class="rounded-circle flex-shrink-0" style="width:7px;height:7px;background:#6c757d;"></span>
                                <span>WB 1</span>
                                <span id="wb-native-1-priority" class="badge rounded-pill bg-info bg-opacity-25 text-info border border-info border-opacity-50 d-none" style="font-size:0.52rem;">Prio</span>
                            </div>
                            <div class="text-truncate" style="font-size:0.75rem;"><span id="wb-native-1-amp" class="text-info fw-bold">0 A</span> <span id="wb-native-1-phase" class="badge bg-secondary bg-opacity-25 text-secondary ms-1" style="font-size:0.6rem;">--p</span> <span class="text-muted mx-1">|</span> <span id="wb-native-1-state" class="text-muted">Idle</span></div>
                        </div>
                        <div id="wb-native-2-slot" class="flex-fill rounded-2 px-2 py-1 wb-native-slot" style="border:1px solid rgba(108,117,125,0.24); background:rgba(108,117,125,0.06); min-width:0;">
                            <div class="d-flex align-items-center gap-1 text-muted text-uppercase" style="font-size:0.55rem;">
                                <span id="wb-native-2-dot" class="rounded-circle flex-shrink-0" style="width:7px;height:7px;background:#6c757d;"></span>
                                <span>WB 2</span>
                                <span id="wb-native-2-priority" class="badge rounded-pill bg-info bg-opacity-25 text-info border border-info border-opacity-50 d-none" style="font-size:0.52rem;">Prio</span>
                            </div>
                            <div class="text-truncate" style="font-size:0.75rem;"><span id="wb-native-2-amp" class="text-info fw-bold">0 A</span> <span id="wb-native-2-phase" class="badge bg-secondary bg-opacity-25 text-secondary ms-1" style="font-size:0.6rem;">--p</span> <span class="text-muted mx-1">|</span> <span id="wb-native-2-state" class="text-muted">Idle</span></div>
                        </div>
                    </div>
                </div>

            </div>
        </div>
        <?php endif; ?>

        <!-- Status Cards -->
	        <div id="dashboard-status-cards-home">
        <div class="row g-2 mb-3" id="dashboard-status-cards">
            <!-- PV -->
            <div class="col-md-6 col-xl-3">
                <div class="card h-100" onclick="switchChartMode('live', 'pv')">
                    <div class="card-body d-flex align-items-center p-3">
                        <div class="icon-box bg-warning bg-opacity-10 text-warning me-3" id="icon-pv-box">
                            <i class="fas fa-sun" id="icon-pv"></i>
                        </div>
                        <div class="flex-grow-1 d-flex justify-content-between align-items-center">
                            <div>
                                <div class="val-large text-warning" id="val-pv" style="line-height:1;">--<span class="val-unit">W</span></div>
                                <div id="val-pv-details" style="display:none;" class="small text-muted mt-1 tile-detail">Soll: <span id="val-pv-soll" class="fw-bold">--</span></div>
                                <div id="pv-peak-detail" style="display:none;" class="small text-muted mt-1 tile-detail"><i class="fas fa-arrow-up text-danger opacity-75"></i> <span id="val-pv-max" class="fw-bold">--</span> (Max)</div>
                                <div class="small text-muted mt-1 tile-detail" id="pv-strings-detail" style="display:none; font-size: 0.75rem;"></div>
                            </div>
                            <div class="text-end text-muted d-flex flex-column gap-1" style="line-height: 1;">
                                <div class="badge bg-body-tertiary text-body border border-secondary-subtle" title="Tagesertrag"><i class="fas fa-sun text-warning me-1"></i><span id="pv-yield-today">-- kWh</span></div>
                                <div class="badge bg-body-tertiary text-body border border-secondary-subtle" title="Tagesprognose"><i class="fas fa-chart-line text-info me-1"></i><span id="val-pv-forecast">-- kWh</span></div>
                                <div class="badge bg-body-tertiary text-body border border-secondary-subtle" title="Noch erwartet"><i class="fas fa-magic text-secondary me-1"></i><span id="val-pv-forecast-remain">-- kWh</span></div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            <!-- Battery -->
            <div class="col-md-6 col-xl-3">
                <div class="card h-100" onclick="switchChartMode('live', 'bat')">
                    <div class="card-body d-flex align-items-center p-3">
                        <div class="icon-box bg-success bg-opacity-10 text-success me-3 battery-icon-box" id="icon-bat">
                            <i class="fas fa-battery-half"></i>
                        </div>
                        <div class="flex-grow-1 d-flex justify-content-between align-items-center">
                            <div>
                                <div class="battery-value-row">
                                    <div class="val-large text-success" id="val-bat-container" style="line-height:1;">--<span class="val-unit">W</span></div>
                                    <span id="val-soc" class="battery-soc-chip text-success" title="Hausakku-SoC" aria-label="Hausakku-SoC">--%</span>
                                </div>
                                <div class="small text-muted tile-detail" id="bat-details" style="display:none; font-size: 0.75rem;"></div>
                                <div id="bat-peak-detail" style="display:none;" class="small text-muted mt-1 tile-detail"><i class="fas fa-arrow-down text-success opacity-75" title="Max Laden"></i> <span id="val-bat-max-in" class="fw-bold">--</span> &bull; <i class="fas fa-arrow-up text-danger opacity-75" title="Max Entladen"></i> <span id="val-bat-max-out" class="fw-bold">--</span></div>
                            </div>
                            <div class="text-end d-flex flex-column gap-1 align-items-end battery-meta-stack">
                                <span id="val-bat-time" class="badge bg-body-tertiary text-muted border border-secondary-subtle battery-time-badge is-placeholder" style="font-size: 0.75rem;" aria-label="Keine Batteriezeit">--</span>
                                <span class="badge bg-body-tertiary text-body border border-secondary-subtle tile-kwh-badge" title="Heute geladen"><i class="fas fa-arrow-down text-success me-1"></i><span id="bat-in-today">-- kWh</span></span>
                                <span class="badge bg-body-tertiary text-body border border-secondary-subtle tile-kwh-badge" title="Heute entladen"><i class="fas fa-arrow-up text-danger me-1"></i><span id="bat-out-today">-- kWh</span></span>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            <!-- Home -->
            <div class="col-md-6 col-xl-3">
                <div class="card h-100" onclick="toggleStatsView('desktop')" style="cursor: pointer;" title="Tagesstatistiken öffnen">
                    <div class="card-body d-flex align-items-center p-3">
                        <div class="icon-box bg-info bg-opacity-10 text-info me-3">
                            <i class="fas fa-home"></i>
                        </div>
                        <div class="flex-grow-1 d-flex justify-content-between align-items-center">
                            <div>
                                <div class="val-large text-info" id="val-home" style="line-height:1;">--<span class="val-unit">W</span></div>
                                <div id="home-forecast-details" class="small text-muted mt-1 tile-detail" style="display:none;">KI-Prog: <span id="val-home-forecast" class="fw-bold">--</span></div>
                                <div id="home-peak-detail" style="display:none;" class="small text-muted mt-1 tile-detail"><i class="fas fa-arrow-down text-success opacity-75"></i> <span id="val-home-min" class="fw-bold">--</span> &bull; <i class="fas fa-arrow-up text-danger opacity-75"></i> <span id="val-home-max" class="fw-bold">--</span></div>
                            </div>
                            <div class="text-end text-muted d-flex flex-column gap-1" style="line-height: 1;">
                                <div class="badge bg-body-tertiary text-body border border-secondary-subtle tile-kwh-badge" title="Hausverbrauch heute"><i class="fas fa-home text-info me-1"></i><span id="home-today">-- kWh</span></div>
                                <div class="badge bg-body-tertiary text-body border border-secondary-subtle" title="Autarkie (Live / Heute)"><i class="fas fa-leaf text-success me-1"></i><span id="val-autarky-live">--%</span> <span class="text-muted fw-normal">| <span id="val-autarky-day">--%</span></span></div>
                                <div class="badge bg-body-tertiary text-body border border-secondary-subtle" title="Eigenverbrauch (Heute)"><i class="fas fa-recycle text-warning me-1"></i><span id="val-selfcon-day">--%</span></div>
                                <?php if($climateEnabled): ?>
                                <div class="badge bg-body-tertiary text-body border border-info border-opacity-50 tile-kwh-badge home-climate-badge" title="Klimaverbrauch heute"><i class="fas fa-snowflake text-info me-1"></i><span id="climate-today">-- kWh</span></div>
                                <?php endif; ?>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            <!-- Grid -->
            <div class="col-md-6 col-xl-3">
                <div class="card h-100" onclick="switchChartMode('live', 'grid')">
                    <div class="card-body d-flex align-items-center p-3">
                        <div class="icon-box bg-secondary bg-opacity-10 text-secondary me-3" id="icon-grid">
                            <i class="fas fa-network-wired"></i>
                        </div>
                        <div class="flex-grow-1 d-flex justify-content-between align-items-center" style="min-width: 0;">
                            <div style="min-width: 0; flex-shrink: 1;">
                                <div class="val-large text-body text-truncate d-flex align-items-baseline" id="val-grid-container" style="line-height:1;">--<span class="val-unit">W</span></div>
	                                <div class="d-flex gap-1 flex-wrap mt-1 status-tile-meta grid-kwh-meta" id="grid-kwh-meta">
                                    <span class="badge bg-body-tertiary text-body border border-secondary-subtle tile-kwh-badge" title="Netzbezug heute"><i class="fas fa-arrow-down text-danger me-1"></i><span id="grid-in-today">-- kWh</span></span>
                                    <span class="badge bg-body-tertiary text-body border border-secondary-subtle tile-kwh-badge" title="Einspeisung heute"><i class="fas fa-arrow-up text-success me-1"></i><span id="grid-out-today">-- kWh</span></span>
                                </div>
                                <div class="small text-muted mt-1 tile-detail text-truncate" id="grid-details" style="display:none; font-size: 0.75rem; line-height: 1.2;"></div>
                                <div id="grid-peak-detail" style="display:none;" class="small text-muted mt-1 tile-detail text-truncate" title="Zähler Max Peaks des Tages"><i class="fas fa-arrow-down text-danger opacity-75" title="Max Bezug"></i> <span id="val-grid-max-in" class="fw-bold">--</span> &bull; <i class="fas fa-arrow-up text-success opacity-75" title="Max Einspeisung"></i> <span id="val-grid-max-out" class="fw-bold">--</span></div>
                            </div>
                            <div class="text-end d-flex flex-column gap-1 align-items-end ms-1 flex-shrink-0" id="card-price-container" style="display:none !important; max-width: 45%;">
                                <div class="badge bg-body-tertiary text-body border border-secondary-subtle py-1 px-2 d-flex align-items-center gap-1 justify-content-center" title="Strompreis & Tendenz" style="cursor: pointer; min-width: 70px;" onclick="event.stopPropagation(); switchChartMode('price');">
                                    <div id="val-price" class="fw-bold" style="font-size: 1.05rem; line-height: 1;">--</div>
                                    <div id="price-trend" class="text-body-secondary d-flex align-items-center" style="font-size: 0.85rem;"><i class="fas fa-minus"></i></div>
                                </div>
                                <div id="val-eco-container" class="badge bg-success bg-opacity-10 text-success border border-success border-opacity-50 px-2 py-1 align-items-center gap-1" style="display:none; cursor: pointer;" onclick="event.stopPropagation(); window.location.href='index.php?seite=config';" title="Eco-Score (0-100: Gibt an, wie stark die Wärmepumpe/Wallbox das Laden priorisiert)">
                                    <i class="fas fa-brain"></i> Score: <span id="val-eco-score">--</span>
                                </div>

                                <!-- Verborgen, aber für JS-Kompatibilität erhalten -->
                                <div style="display:none;">
                                    <div id="price-bar"></div>
                                    <span id="val-price-min"></span>
                                    <span id="val-price-max"></span>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        </div>

        <!-- Main Content Area -->
	        <div class="row g-2" id="dashboard-main-layout">
	            <!-- Left Column: Chart -->
	            <div class="col-xl-9 col-lg-8" id="dashboard-chart-column">
                <div class="card h-100 position-relative overflow-hidden">
                    <div class="card-header bg-transparent border-secondary d-flex justify-content-between align-items-center py-3">
                        <div class="d-flex align-items-center gap-3">
                            <h6 class="mb-0 fw-bold text-nowrap" id="chart-title"><i class="fas fa-chart-line me-2 text-secondary"></i>SoC Prognose</h6>

                            <!-- Header Info für Lade-Fahrplan (Nur auf großen Bildschirmen) -->
                            <div id="header-regler-plan" class="small d-none align-items-center gap-3 border-start border-secondary ms-2 ps-3 fw-bold" style="font-size: 0.8rem;">
                                <span id="head-rb-wrap" title="Startanker / Kurvenbeginn"><i class="fas fa-play text-success opacity-75"></i> <span id="head-rb" class="text-body">--:--</span></span>
                                <span id="head-re-wrap" title="Aktives Regelziel"><i id="head-re-icon" class="fas fa-bullseye text-success opacity-75"></i> <span id="head-re" class="text-body">--</span></span>
                                <span id="head-le-wrap" title="Tagesziel / Freilauf"><i class="fas fa-flag-checkered text-info opacity-75"></i> <span id="head-le" class="text-body">--:--</span></span>
                            </div>
                        </div>

                        <div class="d-flex gap-2 align-items-center">
                            <!-- Ansicht Auswahl -->
                            <select class="form-select form-select-sm border-secondary" id="chart-mode-select" style="width: auto; min-width: 140px;" onchange="switchChartMode(this.value)">
                                <option value="hybrid" selected>Hybrid (Live+Prog)</option>
                                <option value="live">Nur Live-Verlauf</option>
                                <option value="price">Strompreis & Kosten</option>
                                <option value="forecast">Nur Prognose</option>
                            </select>

                            <!-- Live Controls -->
                            <div class="gap-2" id="live-controls" style="display:none;">
                                <div class="btn-group btn-group-sm btn-group-custom" role="group">
                                    <button type="button" class="btn btn-outline-secondary active" onclick="updateChart(6, this)">6h</button>
                                    <button type="button" class="btn btn-outline-secondary" onclick="updateChart(12, this)">12h</button>
                                    <button type="button" class="btn btn-outline-secondary" onclick="updateChart(24, this)">24h</button>
                                    <button type="button" class="btn btn-outline-secondary" onclick="updateChart(48, this)">48h</button>
                                </div>
                                <select class="form-select form-select-sm border-secondary" id="history-select-normal" style="max-width: 130px;" onchange="updateChartHistory(this.value)">
                                    <option value="" selected>Live</option>
                                    <?php foreach ($historyFiles as $hf): ?>
                                        <option value="<?= htmlspecialchars($hf['file']) ?>"><?= htmlspecialchars($hf['label']) ?></option>
                                    <?php endforeach; ?>
                                </select>
                                <select class="form-select form-select-sm border-secondary" id="history-select-wp" style="max-width: 130px; display:none;" onchange="updateChartHistory(this.value)">
                                    <option value="" selected>Live</option>
                                    <?php foreach ($luxtronikFiles as $lf): ?>
                                        <option value="<?= htmlspecialchars($lf['file']) ?>"><?= htmlspecialchars($lf['label']) ?></option>
                                    <?php endforeach; ?>
                                </select>
                            </div>

                            <button class="btn btn-sm btn-outline-secondary btn-chart-flip" onclick="toggleChartFlip()" title="Werte klappen (Absolutwerte anzeigen)">
                                <i class="fas fa-arrows-alt-v"></i>
                            </button>
                            <button class="btn btn-sm btn-outline-secondary" onclick="refreshData()" id="main-refresh-btn" title="Aktualisieren">
                                <i class="fas fa-sync-alt"></i>
                            </button>
                        </div>
                    </div>
                    <div id="diagramDetails" class="px-3 pb-2 text-info small fw-bold" style="display:none;"></div>
                    <div id="forecast-kwh-summary" class="forecast-summary px-3 pb-2 text-info small fw-bold" style="display:none;"></div>
                    <div id="pv-forecast-diagnostic-card" class="mx-3 mb-2 rounded border border-secondary-subtle bg-body-tertiary px-3 py-2 small" style="display:none !important;" hidden>
                        <div class="d-flex flex-wrap align-items-center justify-content-between gap-2">
                            <span class="fw-bold"><i class="fas fa-chart-bar text-info me-1"></i>PV-Prognosediagnose</span>
                            <span id="pv-forecast-diagnostic-status" class="badge text-bg-secondary">Noch keine Auswertung</span>
                        </div>
                        <div class="d-flex flex-wrap gap-2 gap-lg-3 mt-1 text-body">
                            <span title="Typischer absoluter Unterschied je verglichenem 15-Minuten-Fenster">Trefferabweichung: <strong id="pv-forecast-diagnostic-hit">–</strong></span>
                            <span title="Positiv bedeutet im Mittel mehr, negativ weniger Ertrag als vorhergesagt">Richtungsversatz: <strong id="pv-forecast-diagnostic-direction">–</strong></span>
                            <span title="Gesamtabweichung, gewichtet nach der tatsächlich erzeugten Energie">Energieabweichung: <strong id="pv-forecast-diagnostic-energy">–</strong></span>
                            <span title="Anteil der archivierten Prognosefenster mit gültigem Messwert">Abdeckung: <strong id="pv-forecast-diagnostic-coverage">–</strong></span>
                        </div>
                        <div class="d-flex flex-wrap justify-content-between gap-2 mt-1 text-muted">
                            <span id="pv-forecast-diagnostic-sample">Noch keine vergleichbaren Fenster</span>
                            <span>Nur Diagnose – ändert keine Regelung und wählt kein Modell aus.</span>
                        </div>
                        <div id="pv-forecast-diagnostic-contract" class="mt-1 text-warning">
                            Punktprognose – kein belegtes P50.
                        </div>
                        <div id="pv-forecast-diagnostic-horizons" class="mt-1 text-muted">
                            Erfassungs-Vorlauf: noch keine revisionsgebundenen Stichproben.
                        </div>
                    </div>
                    <div id="primaryChartSurface" class="card-body p-0 chart-container position-relative">
                        <!-- Live JS Chart Overlay -->
                        <div id="liveChartContainer" class="w-100 h-100 position-absolute top-0 start-0 p-3" style="background-color: var(--bs-card-bg); z-index: 10;">
                            <canvas id="liveChartCanvas"></canvas>
                        </div>

                        <!-- Live Energiefluss (Desktop Layout) -->
                        <?= renderEnergyFlow('desktop', '', 'style="display: none;"') ?>
                    </div>
                    <div id="directMarketingForecastSurface" class="px-3 pb-3" style="display:none; height:280px;">
                        <div class="d-flex flex-wrap align-items-center gap-2 small mb-2">
                            <span class="fw-bold" style="color:#8b5cf6;">Direktvermarktung – ausgewählter Fahrplan</span>
                            <span id="directMarketingForecastState" class="text-muted"></span>
                        </div>
                        <div class="position-relative" style="height:240px;"><canvas id="directMarketingForecastChart"></canvas></div>
                    </div>

                    <!-- Statistics View Overlay (now covers the whole card including header) -->
                    <div id="stats-view" class="w-100 h-100 position-absolute top-0 start-0 p-4" style="display: none; background-color: var(--bs-card-bg); overflow-y: auto; z-index: 30;">
                        <div class="d-flex justify-content-between align-items-center mb-4">
                            <h5 class="fw-bold m-0"><i class="fas fa-chart-pie text-info me-2"></i>Tagesstatistik Detailauswertung</h5>
                            <div class="d-flex gap-2">
                                <select class="form-select form-select-sm border-secondary" id="stats-history-select" onchange="loadStatsForDate(this.value, 'desktop')">
                                    <option value="today">Heute (Live)</option>
                                    <?php foreach ($historyFiles as $hf): ?>
                                        <option value="<?= htmlspecialchars($hf['file']) ?>"><?= htmlspecialchars($hf['label']) ?></option>
                                    <?php endforeach; ?>
                                </select>
                                <button class="btn btn-sm btn-outline-secondary" onclick="toggleStatsView('desktop')"><i class="fas fa-times"></i></button>
                            </div>
                        </div>

                        <!-- Hidden data carriers for JS -->
                        <span id="stat-grid-out-total" style="display:none;">0 kWh</span>
                        <span id="stat-bat-in-total" style="display:none;">0 kWh</span>
                        <!-- Interactive Charts Row -->
                        <div class="row mb-4 g-3 justify-content-center align-items-stretch">
                            <div class="col-md-4">
                                <h6 class="text-muted small text-uppercase fw-bold mb-2 text-center">Energie-Quellen (Mix)</h6>
                                <div class="d-flex align-items-center justify-content-center gap-3">
                                    <div style="height: 140px; width: 140px; min-width: 140px; position: relative; cursor: pointer;" title="Klicke auf ein Segment für Details">
                                        <canvas id="chartMix"></canvas>
                                    </div>
                                    <div class="d-flex flex-column gap-1 small" id="chart-mix-legend">
                                        <div class="d-flex align-items-center gap-2 fw-bold" style="cursor:pointer;" onclick="showDetailCard(0)">
                                            <span style="width:12px;height:12px;border-radius:3px;background:#ffc107;display:inline-block;"></span>
                                            <span>☀ PV: <span id="stat-mix-pv" class="text-warning">--</span> kWh</span>
                                        </div>
                                        <div class="d-flex align-items-center gap-2 fw-bold text-muted" style="font-size:0.85em;">
                                            <span style="width:12px;height:12px;border-radius:3px;background:#4dabf7;display:inline-block;"></span>
                                            <span>📤 Einsp: <span id="stat-mix-feedin" class="text-info">--</span> kWh</span>
                                        </div>
                                        <div class="d-flex align-items-center gap-2 fw-bold" style="cursor:pointer;" onclick="showDetailCard(2)">
                                            <span style="width:12px;height:12px;border-radius:3px;background:#dc3545;display:inline-block;"></span>
                                            <span>⚡ Bezug: <span id="stat-mix-grid" class="text-danger">--</span> kWh</span>
                                        </div>
                                        <div class="d-flex align-items-center gap-2 fw-bold text-muted" style="font-size:0.85em;">
                                            <span style="width:12px;height:12px;border-radius:3px;background:#51cf66;display:inline-block;"></span>
                                            <span>🔋↓ Laden: <span id="stat-mix-bat-in" class="text-success">--</span></span>
                                        </div>
                                        <div class="d-flex align-items-center gap-2 fw-bold" style="cursor:pointer;" onclick="showDetailCard(1)">
                                            <span style="width:12px;height:12px;border-radius:3px;background:#198754;display:inline-block;"></span>
                                            <span>🔋↑ Entlad: <span id="stat-mix-bat" class="text-success">--</span> kWh</span>
                                        </div>
                                        <?php if($climateEnabled): ?>
                                        <div class="d-flex align-items-center gap-2 fw-bold text-muted" style="font-size:0.85em;">
                                            <span style="width:12px;height:12px;border-radius:3px;background:#38bdf8;display:inline-block;"></span>
                                            <span><i class="fas fa-snowflake text-info me-1"></i>Klima: <span id="stat-mix-climate" class="text-info">--</span> kWh</span>
                                        </div>
                                        <?php endif; ?>
                                    </div>
                                </div>
                            </div>
                            <!-- CO2-Baum -->
                            <div class="col-md-2 text-center">
                                <h6 class="text-muted small text-uppercase fw-bold mb-2">CO₂ Bilanz</h6>
                                <div class="d-flex flex-column align-items-center justify-content-center" style="height: 140px;">
                                    <div id="co2-tree" style="font-size: 2.5rem; line-height: 1; transition: all 0.5s ease;" title="Der Baum wächst mit deiner Autarkie!">🌱</div>
                                    <div class="mt-1">
                                        <span id="stat-co2-value" class="fw-bold text-success" style="font-size: 1.2rem;">--</span>
                                        <span class="text-muted" style="font-size: 0.7rem;"> kg</span>
                                    </div>
                                    <div class="text-muted" style="font-size: 0.6rem;">CO₂ gespart</div>
                                </div>
                            </div>
                            <div class="col-md-3 text-center">
                                <h6 class="text-muted small text-uppercase fw-bold mb-2">Autarkie</h6>
                                <div style="height: 140px; position: relative;">
                                    <canvas id="chartAutarky"></canvas>
                                    <div class="position-absolute top-50 start-50 translate-middle text-center" style="pointer-events: none;">
                                        <span class="fs-4 fw-bold text-success" id="stat-overlay-autarky">--%</span>
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-3 text-center">
                                <h6 class="text-muted small text-uppercase fw-bold mb-2">Eigenverbrauch</h6>
                                <div style="height: 140px; position: relative;">
                                    <canvas id="chartSelfcon"></canvas>
                                    <div class="position-absolute top-50 start-50 translate-middle text-center" style="pointer-events: none;">
                                        <span class="fs-4 fw-bold text-warning" id="stat-overlay-selfcon">--%</span>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <!-- Detail Cards Row (Interactive Tabs) -->
                        <div class="row g-3 justify-content-center align-items-stretch" id="stats-detail-cards">
                            <div class="col-md-5" id="detail-card-pv">
                                <div class="card bg-body-tertiary border-warning h-100 shadow-sm">
                                    <div class="card-body">
	                                        <h6 class="fw-bold text-warning border-bottom border-warning pb-2 mb-3 d-flex justify-content-between align-items-center">
	                                            <span><i class="fas fa-sun me-2"></i>Sonne (PV)</span>
	                                            <span id="stat-pv-total" class="badge bg-warning text-dark fs-6">-- kWh</span>
	                                        </h6>
	                                        <div class="text-muted small mb-1 fw-bold">Quellen</div>
	                                        <div class="d-flex justify-content-between small mb-2"><span>E3DC-PV:</span> <span id="stat-pv-e3dc" class="fw-bold">-- kWh (--%)</span></div>
	                                        <div class="d-flex justify-content-between small mb-2"><span>Zusatz-WR:</span> <span id="stat-pv-external" class="fw-bold">-- kWh (--%)</span></div>
	                                        <div class="d-flex justify-content-between small mb-3" id="stat-pv-source-rest-row" style="display:none;"><span>Quellenrest:</span> <span id="stat-pv-source-rest" class="fw-bold">-- kWh (--%)</span></div>
	                                        <div class="text-muted small mb-1 fw-bold">Verwendung</div>
	                                        <div class="d-flex justify-content-between small mb-2"><span>In Haus:</span> <span id="stat-pv-home" class="fw-bold">-- kWh (--%)</span></div>
	                                        <div class="d-flex justify-content-between small mb-2"><span>In Batterie:</span> <span id="stat-pv-bat" class="fw-bold">-- kWh (--%)</span></div>
                                        <?php if($wbEnabled): ?>
                                        <div class="d-flex justify-content-between small mb-2"><span>In Wallbox:</span> <span id="stat-pv-wb" class="fw-bold">-- kWh (--%)</span></div>
                                        <?php endif; ?>
                                        <?php if($wpEnabled): ?>
                                        <div class="d-flex justify-content-between small mb-2"><span>In Wärmepumpe:</span> <span id="stat-pv-wp" class="fw-bold">-- kWh (--%)</span></div>
                                        <?php endif; ?>
                                        <?php if($climateEnabled): ?>
                                        <div class="d-flex justify-content-between small mb-2"><span>In Klima:</span> <span id="stat-pv-climate" class="fw-bold">-- kWh (--%)</span></div>
                                        <?php endif; ?>
                                        <div class="d-flex justify-content-between small mb-2"><span>Ins Netz:</span> <span id="stat-pv-grid" class="fw-bold">-- kWh (--%)</span></div>
                                    </div>
                                </div>
                            </div>

                            <div class="col-md-5" id="detail-card-bat" style="display:none;">
                                <div class="card bg-body-tertiary border-success h-100 shadow-sm">
                                    <div class="card-body">
                                        <h6 class="fw-bold text-success border-bottom border-success pb-2 mb-3 d-flex justify-content-between align-items-center">
                                            <span><i class="fas fa-battery-full me-2"></i>Batterie (Entladen)</span>
                                            <span id="stat-bat-total" class="badge bg-success text-body fs-6">-- kWh</span>
                                        </h6>
                                        <div class="d-flex justify-content-between small mb-2"><span>In Haus:</span> <span id="stat-bat-home" class="fw-bold">-- kWh (--%)</span></div>
                                        <?php if($wbEnabled): ?>
                                        <div class="d-flex justify-content-between small mb-2"><span>In Wallbox:</span> <span id="stat-bat-wb" class="fw-bold">-- kWh (--%)</span></div>
                                        <?php endif; ?>
                                        <?php if($wpEnabled): ?>
                                        <div class="d-flex justify-content-between small mb-2"><span>In Wärmepumpe:</span> <span id="stat-bat-wp" class="fw-bold">-- kWh (--%)</span></div>
                                        <?php endif; ?>
	                                        <?php if($climateEnabled): ?>
	                                        <div class="d-flex justify-content-between small mb-2"><span>In Klima:</span> <span id="stat-bat-climate" class="fw-bold">-- kWh (--%)</span></div>
	                                        <?php endif; ?>
	                                        <div class="d-flex justify-content-between small mb-2"><span>Ins Netz/Verkauf:</span> <span id="stat-bat-grid" class="fw-bold">-- kWh (--%)</span></div>
	                                    </div>
	                                </div>
                            </div>

                            <div class="col-md-5" id="detail-card-grid" style="display:none;">
                                <div class="card bg-body-tertiary border-danger h-100 shadow-sm">
                                    <div class="card-body">
                                        <h6 class="fw-bold text-danger border-bottom border-danger pb-2 mb-3 d-flex justify-content-between align-items-center">
                                            <span><i class="fas fa-network-wired me-2"></i>Netzbezug</span>
                                            <span id="stat-grid-total" class="badge bg-danger fs-6">-- kWh</span>
                                        </h6>
                                        <div class="d-flex justify-content-between small mb-2"><span>In Haus:</span> <span id="stat-grid-home" class="fw-bold">-- kWh (--%)</span></div>
                                        <div class="d-flex justify-content-between small mb-2"><span>In Batterie:</span> <span id="stat-grid-bat" class="fw-bold">-- kWh (--%)</span></div>
                                        <?php if($wbEnabled): ?>
                                        <div class="d-flex justify-content-between small mb-2"><span>In Wallbox:</span> <span id="stat-grid-wb" class="fw-bold">-- kWh (--%)</span></div>
                                        <?php endif; ?>
                                        <?php if($wpEnabled): ?>
                                        <div class="d-flex justify-content-between small mb-2"><span>In Wärmepumpe:</span> <span id="stat-grid-wp" class="fw-bold">-- kWh (--%)</span></div>
                                        <?php endif; ?>
                                        <?php if($climateEnabled): ?>
                                        <div class="d-flex justify-content-between small mb-2"><span>In Klima:</span> <span id="stat-grid-climate" class="fw-bold">-- kWh (--%)</span></div>
                                        <?php endif; ?>
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-3" id="detail-card-saved" style="display:none;">
                                <div class="card bg-body-tertiary border-success h-100 shadow-sm">
                                    <div class="card-body py-2 px-3">
                                        <h6 class="fw-bold text-success border-bottom border-success pb-2 mb-2 d-flex justify-content-between align-items-center small">
                                            <span><i class="fas fa-leaf me-2"></i>kWh-Retter</span>
                                            <span id="stat-saved-total" class="badge bg-success text-body">-- kWh</span>
                                        </h6>
                                        <div class="d-flex justify-content-between small mb-1"><span title="Heute vor der Software-Abregelung gerettete Energie">Abregelung:</span> <span id="stat-saved-derating" class="fw-bold">-- kWh</span></div>
                                        <div class="d-flex justify-content-between small mb-1"><span title="Heute oberhalb der Hardware-Wechselrichtergrenze gerettete Energie">AC-Limit:</span> <span id="stat-saved-inverter" class="fw-bold">-- kWh</span></div>
                                        <div id="stat-saved-alltime-row" class="d-flex justify-content-between small mt-1 pt-2 border-top border-secondary border-opacity-10">
                                            <span id="stat-saved-alltime-label" class="text-muted" title="Seit Start der kWh-Retter-Erfassung insgesamt gerettete Energie">Gesamt gerettet:</span>
                                            <span id="stat-saved-total-alltime" class="fw-bold text-success">-- kWh</span>
                                        </div>
                                    </div>
                                </div>
                            </div>
                            <!-- Kosten / Finanzielle Bilanz -->
                            <div class="col-md-4" id="detail-card-costs">
                                <div class="card bg-body-tertiary border-primary h-100 shadow-sm">
                                    <div class="card-body">
                                        <h6 class="fw-bold text-success border-bottom border-success pb-2 mb-3 d-flex justify-content-between align-items-center">
                                            <span><i class="fas fa-euro-sign me-2"></i>Endergebnis</span>
                                            <span id="stat-result-total" class="badge bg-success text-body fs-6">0.00 €</span>
                                        </h6>
                                        <div class="d-flex justify-content-between small mb-2 fw-info"><span>Bezug & Einspeisung:</span> <span id="stat-cost-total" class="text-danger fw-bold">0.00 €</span></div>
                                        <div class="d-flex justify-content-between small mb-2 fw-info"><span>Summe der Ersparnis:</span> <span id="stat-save-total" class="text-info fw-bold">0.00 €</span></div>
                                        <div id="stat-eeg-row" class="d-flex justify-content-between small mb-1" style="display:none;"><span>EEG-Einspeisevergütung:</span> <span id="stat-eeg-total" class="text-success fw-bold">--</span></div>
                                        <div id="stat-eeg-note" class="small text-muted mb-2" style="display:none; font-size: 0.7rem;"></div>
                                        <div id="stat-dv-battery-sale-row" class="d-flex justify-content-between small mb-1" style="display:none;" title="Separater Ist-Erlös aus dem Direktvermarktungs-Tagesreport; nicht in das Endergebnis eingerechnet."><span class="text-muted">DV-Batterieverkauf netto:</span> <span id="stat-dv-battery-sale" class="text-success fw-bold">—</span></div>
                                        <div id="stat-dv-battery-sale-note" class="small text-muted mb-2" style="display:none; font-size: 0.7rem;"></div>
                                        <hr class="my-2 border-secondary opacity-25">
                                        <div class="small fw-bold text-muted mb-2 text-uppercase" style="font-size: 0.7rem;">Aufschlüsselung (Kosten / Ersparnis)</div>
                                        <div class="d-flex justify-content-between small mb-2"><span>🏠 Haus:</span> <span><span id="stat-cost-home" class="fw-bold">0.00 €</span> / <span id="stat-save-home" class="text-info fw-bold">0.00 €</span></span></div>
                                        <?php if($wbEnabled): ?>
                                        <div class="d-flex justify-content-between small mb-2"><span>🔌 Wallbox:</span> <span><span id="stat-cost-wb" class="fw-bold">0.00 €</span> / <span id="stat-save-wb" class="text-info fw-bold">0.00 €</span></span></div>
                                        <?php endif; ?>
                                        <?php if($wpEnabled): ?>
                                        <div class="d-flex justify-content-between small mb-2"><span>🔥 Wärmepumpe:</span> <span><span id="stat-cost-wp" class="fw-bold">0.00 €</span> / <span id="stat-save-wp" class="text-info fw-bold">0.00 €</span></span></div>
                                        <?php endif; ?>
                                        <?php if($climateEnabled): ?>
                                        <div class="d-flex justify-content-between small mb-2"><span><i class="fas fa-snowflake text-info me-1"></i>Klima:</span> <span><span id="stat-cost-climate" class="fw-bold">0.00 €</span> / <span id="stat-save-climate" class="text-info fw-bold">0.00 €</span></span></div>
                                        <?php endif; ?>
                                        <div class="d-flex justify-content-between small mt-2 pt-2 border-top border-secondary border-opacity-10">
                                            <span class="text-muted">Ø Strompreis (bezogen):</span>
                                            <span id="stat-avg-price" class="fw-bold">0.0 ct/kWh</span>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

	            <!-- Right Column: Details & Controls -->
	            <div class="col-xl-3 col-lg-4" id="dashboard-side-column">
                <div class="d-flex flex-column gap-2 h-100" id="right-column-cards">
                    <div id="right-compact-badges" class="dashboard-compact-badge-stack">
                        <div id="right-compact-main-badges" class="dashboard-compact-badge-column"></div>
                        <div id="right-compact-consumer-badges" class="dashboard-compact-badge-column"></div>
                    </div>

                    <?php
                    $dashWbCfg = $confData['config'] ?? [];
                    $dashHasWb1 = hasWallbox1Config($dashWbCfg);
                    $dashHasWb2 = hasWallbox2Config($dashWbCfg);
                    $dashWbTypeLabels = [
                        'openwb' => 'openWB',
                        'openwb_pro' => 'openWB Pro',
                        'goe' => 'go-e',
                        'go-e' => 'go-e',
                        'e3dc' => 'E3DC Easy',
                        'e3dc_auto' => 'E3DC Wallbox Auto',
                        'e3dc_efy' => 'E3DC Wallbox efy',
                        'e3dc_easy' => 'E3DC Easy Connect',
                        'e3dc_easy_connect' => 'E3DC Easy Connect',
                        'e3dc_multi' => 'E3DC Multi',
                        'e3dc_multi_connect' => 'E3DC Multi',
                        'shelly' => 'Shelly',
                        'tibber' => 'Tibber Pulse',
                        'fronius' => 'Fronius'
                    ];
                    $dashWb1TypeRaw = normalizeWallboxTypeConfig($dashWbCfg['wb_native_type'] ?? 'e3dc');
                    $dashWb2TypeRaw = normalizeWallboxTypeConfig($dashWbCfg['wb_native_type2'] ?? 'wb2');
                    $dashWb1Type = $dashWbTypeLabels[$dashWb1TypeRaw] ?? 'Wallbox';
                    $dashWb2Type = $dashWbTypeLabels[$dashWb2TypeRaw] ?? 'Wallbox';
                    $dashFlowLabels = getEnergyFlowUiConfig()['labels'] ?? [];
                    $dashWb1ConfiguredName = sanitizeEnergyFlowLabel($dashWbCfg['wb1_name'] ?? '');
                    $dashWb2ConfiguredName = sanitizeEnergyFlowLabel($dashWbCfg['wb2_name'] ?? '');
                    $dashWb1Title = sanitizeEnergyFlowLabel($dashFlowLabels['wallbox'] ?? '')
                        ?: (($dashWb1ConfiguredName !== '' && strlen((string)($dashWbCfg['wb1_name'] ?? '')) <= 32) ? $dashWb1ConfiguredName : 'Wallbox 1');
                    $dashWb2Title = sanitizeEnergyFlowLabel($dashFlowLabels['wallbox2'] ?? '')
                        ?: (($dashWb2ConfiguredName !== '' && strlen((string)($dashWbCfg['wb2_name'] ?? '')) <= 32) ? $dashWb2ConfiguredName : 'Wallbox 2');
                    ?>
                    <!-- Wallbox Card 1 -->
                    <?php if($dashHasWb1): ?>
                    <div class="right-card-wrapper dashboard-consumer-badge dashboard-wallbox-badge" id="card-wb-wrapper">
                        <div class="card" onclick="switchChartMode('live', 'wb')" style="cursor:pointer;">
                            <div class="card-body d-flex align-items-center p-3" style="min-height: 114px;">
                                <div class="icon-box bg-secondary bg-opacity-10 text-secondary me-3 position-relative" id="icon-wb">
                                    <i class="fas fa-charging-station"></i>
                                    <i class="fas fa-lock position-absolute text-warning" id="wb-lock-overlay" style="font-size: 0.7rem; bottom: 8px; right: 8px; display: none;"></i>
                                </div>
                                <div class="flex-grow-1">
                                    <div class="d-flex justify-content-between align-items-center">
                                        <div class="wallbox-card-main">
                                            <div class="small fw-bold text-info text-truncate mb-1 tile-detail" id="wb-title" title="<?= htmlspecialchars($dashWb1Title) ?>"><i class="fas fa-charging-station me-1"></i><?= htmlspecialchars($dashWb1Title) ?></div>
                                            <div class="d-flex align-items-baseline gap-2 mb-1">
                                                <div class="val-large text-body" id="val-wb" style="line-height:1;">0<span class="val-unit">W</span></div>
                                            </div>
                                            <div class="small text-muted text-truncate tile-detail" id="wb-status">Bereit</div>
                                            <div class="small text-muted tile-detail wallbox-identity-detail" id="wb-identity" style="display:none; font-size:0.7rem;"></div>
                                            <div class="small text-muted mt-1 tile-detail" id="wb-details" style="display:none; font-size: 0.7rem;"></div>
                                            <div id="wb-peak-detail" style="display:none;" class="small text-muted mt-1 tile-detail"><i class="fas fa-arrow-up text-danger opacity-75"></i> <span id="val-wb-max" class="fw-bold">--</span> (Max)</div>
                                        </div>
                                        <div class="d-flex flex-column align-items-end gap-1 ms-2 wallbox-meta-stack">
                                            <button type="button" id="wb-pause-toggle" class="btn btn-sm btn-outline-secondary rounded-circle wallbox-pause-btn" data-dashboard-wb-pause="1" data-paused="0" title="Wallbox manuell pausieren" aria-label="Wallbox 1 pausieren" style="width:2rem;height:2rem;display:inline-flex;align-items:center;justify-content:center;">
                                                <i class="fas fa-pause"></i>
                                            </button>
                                            <span id="val-car-soc" class="badge text-bg-success fw-bold wallbox-car-badge" style="display:none; cursor: pointer; font-size: 0.8em;" onclick="event.stopPropagation(); forceSocUpdate()" title="SoC vom Auto abrufen"></span>
                                            <span id="wb-daily" class="badge bg-body-tertiary text-body border border-secondary-subtle tile-kwh-badge w-100 text-end" title="Wallbox 1 heute"><i class="fas fa-calendar-day text-info me-1"></i><span id="wb-daily-value">-- kWh</span></span>
                                            <div id="wb-session-container" class="d-flex flex-column align-items-end gap-1" style="display:none; font-size: 0.8em;">
                                                <span id="wb-kva" class="badge bg-body-tertiary text-info border border-info border-opacity-50 w-100 text-end" style="display:none;" title="Scheinleistung: Spannung x Strom je Phase. Der grosse Wert bleibt die Wirkleistung in W."></span>
                                                <span id="wb-session" class="badge bg-body-tertiary text-body border border-secondary-subtle w-100 text-end" style="display:none;"></span>
                                                <div class="d-flex gap-1 w-100 justify-content-end">
                                                    <span id="wb-time-target" class="badge bg-body-tertiary text-body border border-secondary-subtle" style="display:none;"></span>
                                                    <span id="wb-time-full" class="badge bg-body-tertiary text-body border border-secondary-subtle" style="display:none;"></span>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                    <?php endif; ?>

                    <!-- Wallbox Card 2 -->
                    <?php if($dashHasWb2): ?>
                    <div class="right-card-wrapper dashboard-consumer-badge dashboard-wallbox-badge" id="card-wb2-wrapper" style="display:none;">
                        <div class="card" onclick="switchChartMode('live', 'wb2')" style="cursor:pointer;">
                            <div class="card-body d-flex align-items-center p-3" style="min-height: 114px;">
                                <div class="icon-box bg-secondary bg-opacity-10 text-secondary me-3 position-relative" id="icon-wb2">
                                    <i class="fas fa-charging-station"></i>
                                    <i class="fas fa-lock position-absolute text-warning" id="wb2-lock-overlay" style="font-size: 0.7rem; bottom: 8px; right: 8px; display: none;"></i>
                                </div>
                                <div class="flex-grow-1">
                                    <div class="d-flex justify-content-between align-items-center">
                                        <div class="wallbox-card-main">
                                            <div class="small fw-bold text-info text-truncate mb-1 tile-detail" id="wb2-title" title="<?= htmlspecialchars($dashWb2Title) ?>"><i class="fas fa-charging-station me-1"></i><?= htmlspecialchars($dashWb2Title) ?></div>
                                            <div class="d-flex align-items-baseline gap-2 mb-1">
                                                <div class="val-large text-body" id="val-wb2" style="line-height:1;">0<span class="val-unit">W</span></div>
                                            </div>
                                            <div class="small text-muted text-truncate tile-detail" id="wb2-status">Bereit</div>
                                            <div class="small text-muted tile-detail wallbox-identity-detail" id="wb2-identity" style="display:none; font-size:0.7rem;"></div>
                                            <div class="small text-muted mt-1 tile-detail" id="wb2-details" style="display:none; font-size: 0.7rem;"></div>
                                            <div id="wb2-peak-detail" style="display:none;" class="small text-muted mt-1 tile-detail"><i class="fas fa-arrow-up text-danger opacity-75"></i> <span id="val-wb2-max" class="fw-bold">--</span> (Max)</div>
                                        </div>
                                        <div class="d-flex flex-column align-items-end gap-1 ms-2 wallbox-meta-stack">
                                            <button type="button" id="wb2-pause-toggle" class="btn btn-sm btn-outline-secondary rounded-circle wallbox-pause-btn" data-dashboard-wb-pause="2" data-paused="0" title="Wallbox manuell pausieren" aria-label="Wallbox 2 pausieren" style="width:2rem;height:2rem;display:inline-flex;align-items:center;justify-content:center;">
                                                <i class="fas fa-pause"></i>
                                            </button>
                                            <span id="val-car-soc2" class="badge text-bg-success fw-bold wallbox-car-badge" style="display:none; cursor: pointer; font-size: 0.8em;" onclick="event.stopPropagation(); forceSocUpdate()" title="SoC vom Auto abrufen"></span>
                                            <span id="wb2-daily" class="badge bg-body-tertiary text-body border border-secondary-subtle tile-kwh-badge w-100 text-end" title="Wallbox 2 heute"><i class="fas fa-calendar-day text-info me-1"></i><span id="wb2-daily-value">-- kWh</span></span>
                                            <div id="wb2-session-container" class="d-flex flex-column align-items-end gap-1" style="display:none; font-size: 0.8em;">
                                                <span id="wb2-kva" class="badge bg-body-tertiary text-info border border-info border-opacity-50 w-100 text-end" style="display:none;" title="Scheinleistung: Spannung x Strom je Phase. Der grosse Wert bleibt die Wirkleistung in W."></span>
                                                <span id="wb2-session" class="badge bg-body-tertiary text-body border border-secondary-subtle w-100 text-end" style="display:none;"></span>
                                                <div class="d-flex gap-1 w-100 justify-content-end">
                                                    <span id="wb2-time-target" class="badge bg-body-tertiary text-body border border-secondary-subtle" style="display:none;"></span>
                                                    <span id="wb2-time-full" class="badge bg-body-tertiary text-body border border-secondary-subtle" style="display:none;"></span>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                    <?php endif; ?>

                    <!-- Heatpump Card -->
                    <?php if($wpEnabled): ?>
                    <div class="right-card-wrapper dashboard-consumer-badge" id="card-wp-wrapper">
                        <div class="card" <?php if($wpEnabled): ?>onclick="switchChartMode('live', 'wp')" style="cursor:pointer;"<?php endif; ?>>
                            <div class="card-body d-flex align-items-center p-3" style="min-height: 114px;">
                                <div class="icon-box bg-danger bg-opacity-10 text-danger me-3" id="icon-wp">
                                    <i class="fas fa-fire"></i>
                                </div>
                                <div class="flex-grow-1">
                                    <div class="d-flex justify-content-between align-items-center">
                                        <div>
                                            <div class="val-large text-body" id="val-wp" style="line-height:1;">0<span class="val-unit">W</span></div>
                                            <div id="wp-peak-detail" style="display:none;" class="small text-muted mt-1 tile-detail"><i class="fas fa-arrow-up text-danger opacity-75"></i> <span id="val-wp-max" class="fw-bold">--</span> (Max)</div>
                                        </div>
                                        <div class="d-flex flex-column align-items-end gap-1 ms-2">
                                            <?php if(file_exists('/var/www/html/ramdisk/manual_boost.flag')): ?>
                                                <span class="badge bg-warning text-dark pulsating border w-100 text-end" title="Batterie leeren aktiv"><i class="fas fa-bolt"></i> Boost</span>
                                            <?php endif; ?>
                                            <span id="wp-today" class="badge bg-body-tertiary text-body border border-secondary-subtle tile-kwh-badge w-100 text-end" title="Wärmepumpe heute"><i class="fas fa-calendar-day text-danger me-1"></i><span id="wp-today-value">-- kWh</span></span>
                                            <span id="wp-morning-boost" class="badge bg-warning text-dark border w-100 text-end" style="display:none;" title="Morning Boost aktiv"></span>
                                            <span id="wp-sg-ready-badge" class="badge border w-100 text-end" style="display:none;" title="Bestätigter SG-Ready-Aktorstatus"></span>
                                            <span id="wp-season-badge" class="badge bg-secondary text-white border w-100 text-end" style="display:none;" title="Heiz-/Sommerbetrieb"></span>
                                            <span id="wp-status-badge" class="badge w-100 text-end w-auto" style="display:none;<?= $wpEnabled ? ' cursor:pointer;' : '' ?>" <?= $wpEnabled ? 'onclick="event.stopPropagation(); window.location.href=\'index.php?seite=waermepumpe\'"' : '' ?>></span>
                                        </div>
                                    </div>
                                    <div class="d-flex justify-content-between align-items-end mt-1">
                                        <div id="wp-forecast-details" class="small text-muted tile-detail" style="display:none; font-size: 0.75rem;">KI-Prog: <span id="val-wp-forecast" class="fw-bold">--</span></div>
                                        <div class="text-end small text-muted d-flex gap-2 tile-detail" id="wp-temps" style="display:none; font-size: 0.75rem;">
                                            <div>WW: <span id="val-wp-ww" class="fw-bold text-body">--</span>°C</div>
                                            <div id="wp-rl-container"><span id="wp-rl-label">RL:</span> <span id="val-wp-rl" class="fw-bold text-body">--</span>°C</div>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                    <?php endif; ?>

                    <!-- Klima Card -->
                    <?php if($climateEnabled): ?>
                    <div class="right-card-wrapper dashboard-consumer-badge" id="card-climate-wrapper">
                        <div class="card" onclick="switchChartMode('live', 'climate')" style="cursor:pointer;">
                            <div class="card-body d-flex align-items-center p-3" style="min-height: 114px;">
                                <div class="icon-box bg-info bg-opacity-10 text-info me-3" id="icon-climate-card">
                                    <i class="fas fa-snowflake"></i>
                                </div>
                                <div class="flex-grow-1">
                                    <div class="d-flex justify-content-between align-items-center">
                                        <div>
                                            <div class="val-large text-body" id="val-climate-card" style="line-height:1;">0<span class="val-unit">W</span></div>
                                            <div id="climate-card-status" class="small text-muted mt-1 tile-detail" style="display:none; font-size: 0.75rem;"></div>
                                        </div>
                                        <div class="d-flex flex-column align-items-end gap-1 ms-2">
                                            <span id="climate-card-today" class="badge bg-body-tertiary text-body border border-info border-opacity-50 tile-kwh-badge w-100 text-end" title="Klimaverbrauch heute"><i class="fas fa-calendar-day text-info me-1"></i><span id="climate-card-today-value">-- kWh</span></span>
                                            <span id="climate-card-link" class="badge bg-info bg-opacity-10 text-info border border-info border-opacity-50 w-100 text-end tile-detail" onclick="event.stopPropagation(); window.location.href='index.php?seite=klima'" title="Klima-Status und Einstellungen öffnen"><i class="fas fa-sliders-h me-1"></i>Klima</span>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                    <?php endif; ?>

                    <!-- Heizstab Card -->
                    <?php
                    $hsCfg = $confData['config'] ?? [];
                    $hsCardFile = '/var/www/html/ramdisk/heizstab_data.json';
                    $hsFresh = file_exists($hsCardFile) && (time() - filemtime($hsCardFile)) < 90;
                    $hsCardData = [];
                    if ($hsFresh) {
                        $hsCardData = json_decode(file_get_contents($hsCardFile), true) ?: [];
                    }
                    $hsEnabled = isHeaterEnabledConfig($hsCfg);
                    if ($hsEnabled) {
                        $hsEnabled = $hsEnabled || (!empty($hsCardData['success']) && isset($hsCardData['Heizstab_Power']));
                    }
                    if($hsEnabled): ?>
                    <div class="right-card-wrapper dashboard-consumer-badge" id="card-hs-wrapper">
                        <div class="card" onclick="window.location.href='index.php?seite=waermepumpe'" style="cursor:pointer;">
                            <div class="card-body d-flex align-items-center p-3" style="min-height: 114px;">
                                <div class="icon-box bg-warning bg-opacity-10 text-warning me-3" id="icon-hs-card">
                                    <i class="fas fa-fire-burner"></i>
                                </div>
                                <div class="flex-grow-1">
                                    <div class="d-flex justify-content-between align-items-start">
                                        <div>
                                            <div class="val-large text-body" id="val-hs-card" style="line-height:1;">0<span class="val-unit">W</span></div>
                                            <?php if (!empty($hsCardData['elwa_water_temp_c'])): ?>
                                            <div class="small text-muted mt-1 tile-detail">
                                                <i class="fas fa-thermometer-half me-1"></i>
                                                Wasser: <span class="fw-bold text-body"><?= number_format($hsCardData['elwa_water_temp_c'], 1, ',', '.') ?>°C</span>
                                                <?php if (!empty($hsCardData['elwa_target_temp_c'])): ?>
                                                <span class="text-muted"> / <?= number_format($hsCardData['elwa_target_temp_c'], 1, ',', '.') ?>°C</span>
                                                <?php endif; ?>
                                            </div>
                                            <?php endif; ?>
                                        </div>
                                        <div class="d-flex flex-column align-items-end gap-1 ms-2">
                                            <?php
                                            $elwaStatus = $hsCardData['elwa_status'] ?? '';
                                            $hsMode = $hsCardData['hs_mode'] ?? '';
                                            if ($elwaStatus === 'Heizen' || $elwaStatus === 'Boost'):
                                            ?>
                                                <span id="hs-status-badge" class="badge bg-warning text-dark"><?= htmlspecialchars($elwaStatus) ?></span>
                                            <?php elseif ($elwaStatus === 'Fertig'): ?>
                                                <span id="hs-status-badge" class="badge bg-success text-white"><i class="fas fa-check"></i> Fertig</span>
                                            <?php elseif ($hsMode === 'pre_dump'): ?>
                                                <span id="hs-status-badge" class="badge bg-success text-white"><i class="fas fa-arrow-down"></i> Pre-Dump</span>
                                            <?php elseif ($hsMode === 'grid_follow'): ?>
                                                <span id="hs-status-badge" class="badge bg-success text-white"><i class="fas fa-network-wired"></i> überschuss</span>
                                            <?php elseif ($hsMode === 'pv_auto'): ?>
                                                <span id="hs-status-badge" class="badge bg-info text-dark border w-100 text-end"><i class="fas fa-magic"></i> Auto</span>
                                            <?php else: ?>
                                                <span id="hs-status-badge" class="badge bg-secondary text-white" style="display:none;"></span>
                                            <?php endif; ?>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                    <?php endif; ?>

                    <!-- Ladekurven-Meilensteine Card -->
                    <div class="right-card-wrapper mt-2" id="card-regler-wrapper" style="display:none;">
                        <div class="card h-100 p-1 border-info border-opacity-25" onclick="showStorageCurveModal()" style="cursor:pointer;" title="Klicken für Ladekurven-Details">
                            <div class="card-body py-2 px-3 d-flex flex-column justify-content-center">
                                <h6 class="card-title text-info text-uppercase small fw-bold mb-2 pb-1 border-bottom border-info border-opacity-25 d-flex justify-content-between align-items-center">
                                    <span><i class="fas fa-route me-1"></i> Ladekurve <span id="stat-regler-day" class="badge bg-info bg-opacity-10 text-info border border-info border-opacity-25 ms-1" style="font-size: 0.65rem; font-weight: 600;">Heute</span> <span id="stat-storage-phase" class="badge bg-info bg-opacity-10 text-info border border-info border-opacity-25 ms-1" style="font-size: 0.65rem; font-weight: 600;"></span></span>
                                    <i class="fas fa-chart-area text-info opacity-50" style="font-size:0.75rem;"></i>
                                </h6>
                                <div id="stat-regler-sparkline" class="storage-curve-sparkline" data-state="missing" aria-label="Ladekurvenvorschau">
                                    <svg viewBox="0 0 240 48" preserveAspectRatio="none" aria-hidden="true">
                                        <path class="sparkline-grid" d="M0 36H240 M0 24H240 M0 12H240"></path>
                                        <polyline id="stat-regler-sparkline-target" class="sparkline-target" points=""></polyline>
                                        <polyline id="stat-regler-sparkline-forecast" class="sparkline-forecast" points=""></polyline>
                                    </svg>
                                    <span id="stat-regler-sparkline-state" class="storage-curve-sparkline-state">Keine Plandaten</span>
                                </div>
                                <div class="d-flex justify-content-between small mb-1 align-items-center">
                                    <span class="text-muted"><i class="fas fa-play text-success opacity-75 me-1"></i><span id="stat-regler-rb-label">Kurvenstart:</span></span>
                                    <span class="fw-bold">
                                        <span id="stat-regler-rb-time" class="text-body pe-2">--:--</span>
                                        <span id="stat-regler-rb-soc" class="text-success" style="font-size: 0.8em;">--%</span>
                                    </span>
                                </div>
                                <div class="d-flex justify-content-between small mb-1 align-items-center">
                                    <span class="text-muted"><i class="fas fa-sun text-warning opacity-75 me-1"></i><span id="stat-regler-peak-title">PV-Höchstleistung:</span></span>
                                    <span class="fw-bold">
                                        <span id="stat-regler-re-time" class="text-body pe-2">--:--</span>
                                        <span id="stat-regler-re-soc" class="text-warning" style="font-size: 0.8em;">--%</span>
                                    </span>
                                </div>
                                <div class="d-flex justify-content-between small align-items-center">
                                    <span class="text-muted"><i class="fas fa-flag-checkered text-info opacity-75 me-1"></i>Freilauf ab:</span>
                                    <span class="fw-bold">
                                        <span id="stat-regler-le-time" class="text-body pe-2">--:--</span>
                                        <span id="stat-regler-le-soc" class="text-info" style="font-size: 0.8em;">--%</span>
                                    </span>
                                </div>
                                <div class="mt-2 pt-1 border-top border-secondary border-opacity-10 d-flex justify-content-between align-items-center">
                                    <span class="text-muted" style="font-size:0.7rem;"><span id="stat-regler-soll-label">Jetzt</span>: Soll <span id="stat-regler-soll-now" class="text-info fw-bold">--%</span></span>
                                    <span id="stat-regler-meta" class="text-muted" style="font-size:0.7rem;"></span>
                                </div>
                                <div class="mt-1 d-flex justify-content-between align-items-center gap-2" style="font-size:0.68rem;">
                                    <span id="stat-ems-limits-state" class="text-muted" title="Aktuell gelesene E3DC Power-Settings">EMS --</span>
                                    <span class="text-muted text-end" title="Max. Laden / Max. Entladen laut E3DC Power-Settings">
                                        <i class="fas fa-arrow-down text-success opacity-75 me-1"></i><span id="stat-ems-max-charge">--</span>
                                        <span class="mx-1 opacity-50">|</span>
                                        <i class="fas fa-arrow-up text-danger opacity-75 me-1"></i><span id="stat-ems-max-discharge">--</span>
                                    </span>
                                </div>
                                <div id="stat-regler-summary" class="mt-1 small text-muted" style="font-size:0.72rem; line-height:1.25;"></div>
                            </div>
                        </div>
                    </div>

                    <!-- Quick Actions -->
                    <div class="right-card-wrapper mt-auto">
                        <div class="card h-100">
                            <div class="card-body">
                                <h6 class="card-title text-muted text-uppercase small fw-bold mb-3">Schnellzugriff</h6>
                                <div class="row g-2">
                                    <div class="col-6 col-md-4">
                                        <button class="btn btn-info text-dark w-100 text-start quick-action-btn" data-mode="flow" onclick="switchChartMode('flow')">
                                            <i class="fas fa-project-diagram me-2"></i>E-Flow
                                        </button>
                                    </div>
                                    <div class="col-6 col-md-4">
                                        <button class="btn btn-outline-secondary w-100 text-start quick-action-btn" data-mode="forecast" onclick="switchChartMode('forecast')">
                                            <i class="fas fa-chart-line me-2"></i>Prognose
                                        </button>
                                    </div>
                                    <div class="col-6 col-md-4">
                                        <button class="btn btn-outline-secondary w-100 text-start quick-action-btn" data-mode="hybrid" onclick="switchChartMode('hybrid')">
                                            <i class="fas fa-chart-pie me-2"></i>Hybrid
                                        </button>
                                    </div>
                                    <div class="col-6 col-md-4">
                                        <button class="btn btn-outline-secondary w-100 text-start quick-action-btn" data-mode="price" onclick="switchChartMode('price')">
                                            <i class="fas fa-euro-sign me-2"></i>Kosten
                                        </button>
                                    </div>
                                    <div class="col-6 col-md-4">
                                        <button class="btn btn-outline-secondary w-100 text-start quick-action-btn" data-mode="live" onclick="switchChartMode('live')">
                                            <i class="fas fa-chart-area me-2"></i>Verlauf
                                        </button>
                                    </div>
	                                    <div class="col-6 col-md-4">
	                                        <button class="btn btn-outline-secondary w-100 text-start quick-action-btn btn-diagnose" onclick="showDiagnoseModal()">
	                                            <i class="fas fa-stethoscope me-2"></i>Diagnose
	                                        </button>
	                                    </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        </div>
        <?php elseif ($seite === 'wallbox'): ?>
            <div class="row justify-content-center">
                <div class="col-12 col-lg-10 col-xl-8">
                    <div class="mb-3">
                        <a href="index.php" class="btn btn-outline-secondary btn-sm"><i class="fas fa-arrow-left me-2"></i>Zurück zum Dashboard</a>
                    </div>
                    <?php include 'Wallbox.php'; ?>
                </div>
            </div>
    <?php elseif ($seite === 'lock'): ?>
        <div class="row justify-content-center mt-5 pt-5">
            <div class="col-12 col-md-6 col-lg-4">
                <div class="card shadow-sm border-secondary-subtle" style="border-radius: 16px;">
                    <div class="card-body p-4 text-center">
                        <i class="fas fa-lock text-warning mb-3" style="font-size: 3rem;"></i>
                        <h4 class="fw-bold mb-3">Geschützter Bereich</h4>
                        <p class="text-muted small mb-4">Bitte gib deine PIN ein, um die Steuerung und Konfiguration zu entsperren.</p>
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
                            <button type="submit" class="btn btn-warning w-100 rounded-pill fw-bold">Entsperren</button>
                        </form>
                    </div>
                </div>
            </div>
        </div>
        <?php elseif ($seite === 'matter'): ?>
            <div class="row justify-content-center">
                <div class="col-12 col-xl-10">
                    <div class="mb-3">
                        <a href="index.php" class="btn btn-outline-secondary btn-sm"><i class="fas fa-arrow-left me-2"></i>Zurück zum Dashboard</a>
                    </div>
                    <?php include 'matter.php'; ?>
                </div>
            </div>
        <?php elseif ($seite === 'fahrzeug'): ?>
            <div class="row justify-content-center">
                <div class="col-12 col-lg-10 col-xl-8">
                    <div class="mb-3">
                        <a href="index.php" class="btn btn-outline-secondary btn-sm"><i class="fas fa-arrow-left me-2"></i>Zurück zum Dashboard</a>
                    </div>
                    <?php include 'fahrzeug.php'; ?>
                </div>
            </div>
        <?php elseif ($seite === 'waermepumpe' && ($wpEnabled || $hsEnabled)): ?>
            <div class="row justify-content-center">
                <div class="col-12 col-lg-10 col-xl-8">
                    <div class="mb-3">
                        <a href="index.php" class="btn btn-outline-secondary btn-sm"><i class="fas fa-arrow-left me-2"></i>Zurück zum Dashboard</a>
                    </div>
                    <?php include 'waermepumpe.php'; ?>
                </div>
            </div>
        <?php elseif ($seite === 'klima'): ?>
            <div class="row justify-content-center">
                <div class="col-12 col-lg-10 col-xl-8">
                    <div class="mb-3">
                        <a href="index.php" class="btn btn-outline-secondary btn-sm"><i class="fas fa-arrow-left me-2"></i>Zurück zum Dashboard</a>
                    </div>
                    <?php include 'klima.php'; ?>
                </div>
            </div>
        <?php elseif ($seite === 'vitals'): ?>
            <div class="row justify-content-center">
                <div class="col-12 col-xl-10">
                    <div class="mb-3">
                        <a href="index.php" class="btn btn-outline-secondary btn-sm"><i class="fas fa-arrow-left me-2"></i>Zurück zum Dashboard</a>
                    </div>
                    <?php include 'vitals.php'; ?>
                </div>
            </div>
        <?php elseif ($seite === 'langzeit'): ?>
            <div class="row justify-content-center">
                <div class="col-12 col-lg-10 col-xl-8">
                    <div class="mb-3">
                        <a href="index.php" class="btn btn-outline-secondary btn-sm"><i class="fas fa-arrow-left me-2"></i>Zurück zum Dashboard</a>
                    </div>
                    <?php include 'langzeit.php'; ?>
                </div>
            </div>
        <?php elseif ($seite === 'config'): ?>
            <div class="row justify-content-center">
                <div class="col-12 col-lg-10 col-xl-8">
                    <div class="mb-3 d-flex justify-content-between align-items-center">
                        <a href="index.php" class="btn btn-outline-secondary btn-sm"><i class="fas fa-arrow-left me-2"></i>Zurück</a>
                        <div>
                            <!-- Sicheres System-Update -->
                            <?php if (!$isDocker): ?>
                            <button id="btn-update-installer" class="btn btn-outline-info btn-sm me-2" onclick="startInstallerUpdate()" title="Aktualisiert E3DC-Control über den sicheren Systemjob">
                                <i class="fas fa-sync-alt me-2"></i>System Update <span id="update-badge-installer" class="badge bg-danger ms-1" style="display:none;">!</span>
                            </button>
                            <button class="btn btn-outline-warning btn-sm me-2" onclick="openReleaseRollback()" title="Stabile Rückfallversion auswählen">
                                <i class="fas fa-life-ring me-2"></i>Rückfallversion
                            </button>
                            <?php else: ?>
                            <button class="btn btn-outline-secondary btn-sm me-2" onclick="openReleaseRollback()" title="Zeigt Docker-Befehle für Update und Rückfallversionen an">
                                <i class="fab fa-docker me-2"></i>Docker Versionen
                            </button>
                            <?php endif; ?>
                            <button class="btn btn-outline-warning btn-sm me-2" onclick="fixPermissions()" title="Startet die automatische Prüfung und Reparatur defekter Dateirechte"><i class="fas fa-tools me-2"></i>Rechte reparieren</button>
                            <button class="btn btn-danger btn-sm" onclick="restartService()" title="Setzt alle Dienste sanft zurück und entfernt temporäre Boosts"><i class="fas fa-power-off me-2"></i>Notfall-Neustart (Reset)</button>
                        </div>
                    </div>


                    <?php include 'config_editor.php'; ?>
                </div>
            </div>
        <?php endif; ?>

        <!-- Footer -->
        <footer class="text-center text-muted mt-5 pt-3 border-top border-secondary">
            <small>
                E3DC Control &copy; <?= date('Y') ?> |
                <a href="#" class="text-decoration-none text-secondary" data-bs-toggle="modal" data-bs-target="#changelogModal">Changelog</a>
                | <a href="https://www.photovoltaikforum.com/thread/259876-e3dc-control-native-python-ki-prognose-dynamische-stromtarife-wallbox-steuerung/?action=lastPost" target="_blank" class="text-decoration-none text-secondary" title="Zum neuen E3DC-Control V4 Thread im PV-Forum (letzter Beitrag)"><i class="fas fa-comments"></i> PV-Forum</a>
                | <a href="help.php" class="text-decoration-none text-secondary">FAQ</a>
                <?= renderFooterVersion() ?>
            </small>
        </footer>
    </div>

    <!-- Modals -->
    <?= renderChangelogModal('modal-lg modal-dialog-scrollable') ?>
    <?= renderUpdateModal('modal-lg modal-dialog-scrollable') ?>
    <?= renderReleaseRollbackModal('modal-lg modal-dialog-scrollable') ?>
    <?= renderWatchdogModal('modal-lg modal-dialog-scrollable') ?>
    <?= renderHAModal('modal-lg modal-dialog-scrollable') ?>
    <?= renderDiagnoseModal('modal-lg modal-dialog-scrollable') ?>
    <?= renderGridHealthModal('modal-md modal-dialog-scrollable') ?>

    <!-- Ladekurven-Chart Modal -->
    <div class="modal fade" id="storageCurveModal" tabindex="-1" aria-hidden="true">
      <div class="modal-dialog modal-lg modal-dialog-centered modal-dialog-scrollable">
        <div class="modal-content">
          <div class="modal-header py-2 border-info border-opacity-25">
            <h5 class="modal-title text-info fw-bold"><i class="fas fa-route me-2"></i>Ladekurve <span id="sc-modal-day">Heute</span> <span id="sc-modal-phase" class="badge bg-info bg-opacity-10 text-info border border-info border-opacity-25 ms-2" style="font-size:0.7rem;"></span></h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
          </div>
          <div class="modal-body p-3">
            <!-- Meta-Info Zeile -->
            <div class="d-flex flex-wrap gap-3 mb-3 small" id="sc-meta-row">
              <span class="text-muted">Aktueller SoC: <span id="sc-current-soc" class="fw-bold text-success">--%</span></span>
              <span class="text-muted">Tagesziel: <span id="sc-target-soc" class="fw-bold text-info">--%</span></span>
              <span class="text-muted">Regelziel: <span id="sc-active-target" class="fw-bold text-success">--</span></span>
              <span class="text-muted">Morgen-Puffer: <span id="sc-morning-target" class="fw-bold text-success">--%</span></span>
              <span class="text-muted">Pre-Dump-Min: <span id="sc-predump-min" class="fw-bold text-success">--%</span></span>
              <span class="text-muted">Pre-Dump-Bedarf: <span id="sc-predump-kwh" class="fw-bold text-info">-- kWh</span></span>
              <span class="text-muted">PV-Prognose: <span id="sc-pv-forecast-kwh" class="fw-bold text-warning">-- kWh</span></span>
              <span class="text-muted">Abregeldruck: <span id="sc-curtailment-kwh" class="fw-bold text-info">-- kWh</span></span>
              <span class="text-muted">Headroom: <span id="sc-headroom-kwh" class="fw-bold text-info">--</span></span>
              <span class="text-muted">Tagesziel-Risiko: <span id="sc-evening-risk" class="fw-bold text-success">OK</span></span>
              <span class="text-muted" id="sc-noon-wrap" style="display:none;">Zwischenziele: <span id="sc-noon-target" class="fw-bold text-warning">--%</span></span>
              <span class="text-muted">Max erreichbar: <span id="sc-max-soc" class="fw-bold text-warning">--%</span></span>
              <span id="sc-qratio-wrap" class="text-muted">Kurvenform: <span id="sc-qratio" class="fw-bold">--</span> <i class="fas fa-info-circle" title="Hohe Werte bedeuten: Die Kurve wartet länger auf den eingestellten Freilauf-SoC. Kleine Werte laden früher und direkter."></i></span>
              <span class="text-muted ms-auto">Plan vom: <span id="sc-plan-ts" class="fw-bold">--</span></span>
            </div>
            <!-- Betriebsartabhängige Ladekurve: Standard oder Direktvermarktung -->
            <div id="sc-standard-chart-wrap">
              <div class="small mb-2">
                <span class="fw-bold text-info">Anlagenregelung – Standard-Ladekurve</span>
                <span class="text-muted ms-2">SoC aus PV, Haus und planbaren Lasten; ohne Direktvermarktungswirkung.</span>
              </div>
              <div style="position:relative; height:260px;">
                <canvas id="storageCurveChart"></canvas>
              </div>
            </div>
            <div id="sc-direct-marketing-chart-wrap" style="display:none;">
              <div class="d-flex flex-wrap align-items-center gap-2 mb-2 small">
                <span class="fw-bold" style="color:#8b5cf6;"><i class="fas fa-chart-line me-1"></i>Direktvermarktung – ausgewählter Fahrplan</span>
                <span id="sc-direct-marketing-chart-state" class="text-muted"></span>
              </div>
              <div style="position:relative; height:260px;">
                <canvas id="directMarketingTrajectoryChart"></canvas>
              </div>
            </div>
            <!-- Direktvermarktung -->
            <div id="sc-direct-marketing-section" class="mt-3 p-2 rounded" style="display:none; background:var(--bs-body-bg); border:1px solid rgba(var(--bs-success-rgb),0.22);">
              <div class="small fw-bold text-success mb-2"><i class="fas fa-coins me-1"></i>Direktvermarktung</div>
              <div id="sc-direct-marketing-summary" class="small"></div>
              <div id="sc-direct-marketing-windows" class="small mt-2"></div>
            </div>
            <!-- Aktuelle Situation -->
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
        function updateTime() {
            const now = new Date();
            document.getElementById('clock').innerText = now.toLocaleTimeString('de-DE', {hour: '2-digit', minute:'2-digit'});
            document.getElementById('date').innerText = now.toLocaleDateString('de-DE');
        }
        setInterval(updateTime, 1000);
        updateTime();

        // Konstanten aus logic.php für JS verfügbar machen
        let FORECAST_DATA = <?= json_encode($forecastData) ?>;
        const PV_STRINGS = <?= json_encode($pvStrings) ?>;
        const LAT = <?= json_encode($lat) ?>;
        const LON = <?= json_encode($lon) ?>;
        const BAT_CAPACITY = <?= json_encode($batteryCapacity) ?>;
        const GRID_MAX_AMPS = <?= json_encode($gridMaxAmps) ?>;
        const PV_ATMOSPHERE = <?= json_encode($pvAtmosphere) ?>;
        let SHOW_FORECAST = <?= $showForecast ? 'true' : 'false' ?>;
        let DARK_MODE = <?= $darkMode ? 'true' : 'false' ?>;
        window.E3DC_CSRF_TOKEN = <?= json_encode(e3dcCsrfToken()) ?>;
        window.UI_ENERGY_FLOW = <?= json_encode(getEnergyFlowUiConfig(), JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE) ?>;
        let CURRENT_VIEW = 'normal';
        const INITIAL_CHART_VIEW = <?= json_encode($initialChartView) ?>;
        // Stabile Tages-PV-Gesamtprognose (aus allen h=0..24 Slots, berechnet beim Seitenaufruf)
        const STABLE_PV_TODAY_KWH_PHP = <?= json_encode($stablePvForecastKwh ?? 0) ?>;

        <?php
        $einspeiseW = 0.0;
        $wrW = 0.0;

        if (!empty($_c['einspeiselimit_w'])) {
            $einspeiseW = parseConfigFloat($_c['einspeiselimit_w']);
        } elseif (!empty($_c['einspeiselimit'])) {
            $einspeiseCfg = parseConfigFloat($_c['einspeiselimit']);
            $einspeiseW = ($einspeiseCfg > 100) ? $einspeiseCfg : ($einspeiseCfg * 1000);
        }

        if (!empty($_c['wr_ac_limit_w'])) {
            $wrW = parseConfigFloat($_c['wr_ac_limit_w']);
        } elseif (!empty($_c['wrleistung'])) {
            $wrCfg = parseConfigFloat($_c['wrleistung']);
            $wrW = ($wrCfg > 100) ? $wrCfg : ($wrCfg * 1000);
        }

        $liveLimitFile = '/var/www/html/ramdisk/live_data_py.json';
        if (($einspeiseW <= 0 || $wrW <= 0) && file_exists($liveLimitFile)) {
            $liveLimitData = @json_decode(@file_get_contents($liveLimitFile), true);
            if (is_array($liveLimitData)) {
                if ($einspeiseW <= 0 && !empty($liveLimitData['derate_at_power_w'])) {
                    $einspeiseW = parseConfigFloat($liveLimitData['derate_at_power_w']);
                }
                if ($wrW <= 0 && !empty($liveLimitData['ac_power_limit_w'])) {
                    $wrW = parseConfigFloat($liveLimitData['ac_power_limit_w']);
                }
            }
        }
        ?>
        const E3DC_LIMITS = { einspeise: <?= $einspeiseW ?>, wr: <?= $wrW ?> };
        let SHOW_PEAK_SHAVING = true;


        const DETAIL_MODE_SEQUENCE = ['compact', 'normal', 'detail'];
        const DETAIL_MODE_LABELS = { compact: 'Kompakt', normal: 'Normal', detail: 'Detail' };
        const FRONTEND_DETAIL_MODE_DEFAULT = DETAIL_MODE_SEQUENCE.includes(document.body?.dataset.detailMode)
            ? document.body.dataset.detailMode
            : 'normal';
        let FRONTEND_DETAIL_MODE = FRONTEND_DETAIL_MODE_DEFAULT;
        let SHOW_TILE_DETAILS = FRONTEND_DETAIL_MODE !== 'compact';

        function normalizeFrontendDetailMode(mode) {
            return DETAIL_MODE_SEQUENCE.includes(mode) ? mode : 'normal';
        }

        function applyFrontendDetailMode(mode) {
            const resolved = normalizeFrontendDetailMode(mode);
            FRONTEND_DETAIL_MODE = resolved;
            SHOW_TILE_DETAILS = resolved === 'detail';

            [document.documentElement, document.body].forEach(el => {
                if (!el) return;
                DETAIL_MODE_SEQUENCE.forEach(detailMode => el.classList.remove('detail-' + detailMode));
                el.classList.add('detail-' + resolved);
                el.dataset.detailMode = resolved;
            });
            document.body.classList.toggle('hide-tile-details', resolved !== 'detail');
            syncCompactBadgePlacement(resolved);
            if (resolved === 'compact') {
                const priceContainer = document.getElementById('card-price-container');
                const ecoContainer = document.getElementById('val-eco-container');
                if (priceContainer) priceContainer.style.setProperty('display', 'none', 'important');
                if (ecoContainer) ecoContainer.style.setProperty('display', 'none', 'important');
            }

            const nextMode = DETAIL_MODE_SEQUENCE[(DETAIL_MODE_SEQUENCE.indexOf(resolved) + 1) % DETAIL_MODE_SEQUENCE.length];
            const title = 'Ansicht: ' + DETAIL_MODE_LABELS[resolved] + ' · Klick für ' + DETAIL_MODE_LABELS[nextMode];
            const button = document.getElementById('tiledetails-button');
            const icon = document.getElementById('tiledetails-icon');
            if (button) {
                button.title = title;
                button.setAttribute('aria-label', title);
                button.classList.toggle('is-compact', resolved === 'compact');
                button.classList.toggle('is-normal', resolved === 'normal');
                button.classList.toggle('is-detail', resolved === 'detail');
            }
            if (icon) {
                if (resolved === 'compact') {
                    icon.className = 'fas fa-eye-slash text-secondary';
                } else if (resolved === 'detail') {
                    icon.className = 'fas fa-eye text-danger';
                } else {
                    icon.className = 'fas fa-eye text-primary';
                }
            }
        }

        function toggleTileDetails() {
            const currentIndex = DETAIL_MODE_SEQUENCE.indexOf(FRONTEND_DETAIL_MODE);
            const nextMode = DETAIL_MODE_SEQUENCE[(currentIndex + 1) % DETAIL_MODE_SEQUENCE.length];
            applyFrontendDetailMode(nextMode);
        }

        function syncCompactBadgePlacement(mode) {
            const cards = document.getElementById('dashboard-status-cards');
            const normalHome = document.getElementById('dashboard-status-cards-home');
            const compactMain = document.getElementById('right-compact-main-badges');
            const compactConsumers = document.getElementById('right-compact-consumer-badges');
            if (!cards || !normalHome || !compactMain || !compactConsumers) return;
            const consumerIds = ['card-wb-wrapper', 'card-wb2-wrapper', 'card-wp-wrapper', 'card-climate-wrapper', 'card-hs-wrapper'];
            consumerIds.forEach(id => {
                const el = document.getElementById(id);
                if (!el || document.querySelector('[data-compact-placeholder-for="' + id + '"]')) return;
                const marker = document.createElement('span');
                marker.hidden = true;
                marker.dataset.compactPlaceholderFor = id;
                el.parentNode.insertBefore(marker, el);
            });
            const target = mode === 'compact' ? compactMain : normalHome;
            if (cards.parentElement !== target) {
                target.appendChild(cards);
            }
            consumerIds.forEach(id => {
                const el = document.getElementById(id);
                if (!el) return;
                if (mode === 'compact') {
                    if (el.parentElement !== compactConsumers) compactConsumers.appendChild(el);
                } else {
                    const marker = document.querySelector('[data-compact-placeholder-for="' + id + '"]');
                    if (marker && marker.parentNode && el.parentElement !== marker.parentNode) {
                        marker.parentNode.insertBefore(el, marker.nextSibling);
                    }
                }
            });
        }

        window.addEventListener('DOMContentLoaded', () => {
            applyFrontendDetailMode(FRONTEND_DETAIL_MODE_DEFAULT);
            if (INITIAL_CHART_VIEW === 'climate' && typeof switchChartMode === 'function') {
                setTimeout(() => {
                    switchChartMode('live', 'climate');
                    if (window.history && typeof window.history.replaceState === 'function') {
                        const url = new URL(window.location.href);
                        url.searchParams.delete('view');
                        window.history.replaceState({}, document.title, url.pathname + url.search + url.hash);
                    }
                }, 0);
            }
        });

        // Dark Mode Umschaltung für Desktop (überschreibt evt. solar.js)
        function toggleDarkMode() {
            DARK_MODE = !DARK_MODE;
            // Bootstrap 5 (Desktop) verlangt den Theme-Tag zwingend auf <html> (documentElement)
            document.documentElement.setAttribute('data-bs-theme', DARK_MODE ? 'dark' : 'light');
            document.body.setAttribute('data-bs-theme', DARK_MODE ? 'dark' : 'light');

            const icon = document.getElementById('darkmode-icon');
            if (icon) {
                icon.className = DARK_MODE ? 'fas fa-sun' : 'fas fa-moon';
            }

            fetch('index.php', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'X-CSRF-Token': String(window.E3DC_CSRF_TOKEN || '')
                },
                body: 'action=save_setting&key=darkmode&value=' + (DARK_MODE ? '1' : '0')
            });

            // Explizites Event auslösen, damit Diagramme (langzeit.php) reagieren können
            setTimeout(() => {
                window.dispatchEvent(new CustomEvent('themeChanged'));
            }, 50);
        }

        function fmtPowerShort(value) {
            if (value === null || value === undefined || value === '') return '--';
            const watts = parseFloat(value);
            if (!Number.isFinite(watts)) return '--';
            const absW = Math.abs(watts);
            if (absW >= 1000) {
                const digits = absW >= 10000 ? 0 : 1;
                return (watts / 1000).toLocaleString('de-DE', {
                    minimumFractionDigits: digits,
                    maximumFractionDigits: digits
                }) + ' kW';
            }
            return Math.round(watts).toLocaleString('de-DE') + ' W';
        }

        function updatePeakShaving(data) {
            if (typeof updateDailySavedStats === 'function') {
                updateDailySavedStats(data, '');
            }

            // --- Ladekurven-Meilensteine (storage_manager V4) ---
            // Desktop und Mobilansicht verwenden denselben zusammenführenden
            // Cache. Partielle WebSocket-Telegramme dürfen vollständige
            // Plan-/PV-Kurven aus dem HTTP-Snapshot nicht wieder leeren.
            if (typeof cacheStorageCurveData === 'function') {
                cacheStorageCurveData(data);
            }
            if (Object.prototype.hasOwnProperty.call(data, 'cheap_grid_charge')) {
                window._cheapGridCharge = data.cheap_grid_charge || {};
            } else if (!window._cheapGridCharge) {
                window._cheapGridCharge = {};
            }

            // --- Storage Manager + Simulator Debug Panel (WB Status Bar) ---
            // Nur wenn WB-Banner sichtbar ist (Auto angeschlossen)
            (function() {
                const formatStorageReasonInline = (reason) => {
                    if (!reason) return '--';
                    const text = String(reason);
                    const field = name => {
                        const m = text.match(new RegExp(name + '=([^|\\s]+)'));
                        return m ? m[1].trim() : null;
                    };
                    const bracketPct = name => {
                        const m = text.match(new RegExp('\\[' + name + '\\s+([0-9.,]+)%\\]'));
                        return m ? m[1].replace('.', ',') + ' %-Punkte' : null;
                    };
                    const time = (text.match(/^\[([0-9:]+)\]/) || [])[1] || '--:--';
                    if (text.includes('PRICE_BOOST_GRID')) {
                        return `${time}: Preis-Boost - Speicher lädt im günstigen Stromfenster gezielt; ` +
                               `nach dem Fenster übernimmt wieder die Ladekurve.`;
                    }
                    if (text.includes('AUTO-RUHE') || text.includes('TL_AUTO_QUIET')) {
                        return `${time}: Auto-Ruhe - sichere Ladeleistung ist gerade zu klein oder der Netzpunkt zu unruhig. ` +
                               `Der E3DC regelt kurz autonom; sobald genug PV-Reserve da ist, folgt der Speicher wieder der Ladekurve.`;
                    }
                    if (text.includes('KURVEN-AUTO') || text.includes('PV-Einbruch') || text.includes('tl_curve_auto_relief')) {
                        return `${time}: Kurven-Auto - PV ist im Vergleich zur Prognose stark eingebrochen. ` +
                               `E3DC-AUTO ist freigegeben; Wallbox-Entlastung und Kurven-Dump bleiben gesperrt.`;
                    }
                    if (text.includes('AUTO-FREIGABE') || text.includes('TL_AUTO_RELEASE') || text.includes('TL-AUTO')) {
                        return `${time}: Auto-Freigabe - Tagesziel laut Prognose nicht sicher erreichbar oder heute keine relevante PV mehr. ` +
                               `E3DC-AUTO ist freigegeben; die Ladekurve wird nicht erzwungen.`;
                    }
                    if (text.includes('FREILAUF')) {
                        return `${time}: Freilauf - E3DC arbeitet autonom, kein neuer Steuerbefehl. ` +
                               `SoC ${field('SOC') || '--'}, Ziel ${field('Ziel') || '--'}, PV ${field('PV') || '--'}, Netz ${field('Grid') || '--'}.`;
                    }
                    if (text.includes('WB-KURVENENTLASTUNG') || text.includes('WB-Kurvenentlastung') || text.includes('tl_brake_wb_relief_guard')) {
                        return `${time}: WB-Kurvenentlastung - Speicher liegt oberhalb der Sollkurve und stuetzt die Wallbox ruhig am Netzpunkt.`;
                    }
                    if (text.includes('KURVEN-BREMSE') || text.includes('TL-BREMSE')) return `${time}: Ladekurven-Bremse - Speicher liegt oberhalb der Sollkurve.`;
                    if (text.includes('ABREGELSCHUTZ')) {
                        const catchup = bracketPct('Zielnachlauf') || bracketPct('TL-Zielnachlauf') || bracketPct('Kurvennachlauf');
                        return `${time}: Abregelschutz - PV-Spitze wird in den Speicher gerettet.` +
                               (catchup ? ` Zielnachlauf ${catchup}.` : '');
                    }
                    if (text.includes('PRE-DISCH')) return `${time}: Pre-Dump - Speicher schafft Platz für spätere PV-Spitze.`;
                    if (text.includes('KURVEN-HALT') || text.includes('TL-IDLE')) return `${time}: Kurven-Halt - Speicher liegt am nächsten Kurvenziel; es wird kein aktiver Laderahmen gesetzt.`;
                    if (text.includes('KURVEN-HALTEWAECHTER')) return `${time}: Kurven-Haltewaechter - kurzer Netzbezug erkannt, daher darf der Speicher gegensteuern.`;
                    if (text.includes('KURVEN-DUMP') || text.includes('TL-AUTODUMP')) return `${time}: Kurven-Entladung - Speicher liegt deutlich oberhalb der Kurve und gibt kontrolliert Energie frei.`;
                    if (text.includes('NOTSTROM-AUTO')) return `${time}: Notstrom/Inselbetrieb - E3DC arbeitet autonom, externe Verbraucher-Budgets sind gesperrt.`;
                    if (text.includes('ERHOLUNG-AUTO')) return `${time}: Erholung - E3DC arbeitet autonom, bis der Morgenpuffer wieder erreicht ist.`;
                    if (text.includes('MORGEN-AUTO')) return `${time}: Morgenpuffer - E3DC arbeitet autonom, bis der eingestellte Morgen-SoC erreicht ist.`;
                    if (text.includes('NACHTFREIGABE') || text.includes('TL-NACHTFREIGABE')) return `${time}: Nachtfreigabe - E3DC arbeitet autonom und versorgt das Haus; die Tageskurve wird nachts nicht mehr erzwungen.`;
                    if (text.includes('NETZLADEN') && text.includes('Preis-Boost')) {
                        return `${time}: Preis-Boost - Speicher lädt aus dem Netz bis ${field('Ziel') || '--'} ` +
                               `mit Hysterese ${field('Hyst') || '--'}; PV-Freiraum bleibt reserviert.`;
                    }
                    return text;
                };
                // Storage-Seite
                const storStateEl = document.getElementById('wb-stor-state');
                const storReasonEl = document.getElementById('wb-stor-reason');
                const storSollSocEl = document.getElementById('wb-stor-soll-soc');
                const storIfcEl = document.getElementById('wb-stor-ifc');
                const storCurveEl = document.getElementById('wb-stor-curve');
                const storageOperational = typeof storageOperationalDisplay === 'function'
                    ? storageOperationalDisplay(data)
                    : null;
                const storageForecastBadge = document.getElementById('storage-forecast-badge');
                const storagePlanMeta = window._storagePlanMeta || {};
                const storageCurveMeta = storagePlanMeta.target_curve_meta || {};
                const weatherReserveActive = storagePlanMeta.weather_reserve_active === true || storageCurveMeta.weather_reserve_active === true;
                const weatherReserveNeedWh = parseFloat(storagePlanMeta.weather_reserve_need_wh ?? storageCurveMeta.weather_reserve_need_wh ?? 0) || 0;
                const weatherReserveNeedKwh = weatherReserveNeedWh > 0 ? (weatherReserveNeedWh / 1000).toFixed(1) : null;
                const weatherReserveTarget = storagePlanMeta.planning_target_soc ?? storageCurveMeta.planning_target_soc;
                const weatherBaseTarget = storagePlanMeta.target_soc ?? storageCurveMeta.config_target_soc;
                const weatherReserveText = (() => {
                    let msg = 'Heute kein Pre-Dump: schlechte Prognose. Energie bleibt im Speicher; die Regelung faehrt die Schlechtwetter-Kurve.';
                    if (weatherReserveNeedKwh) msg += ` 48h-Defizit: ${weatherReserveNeedKwh} kWh.`;
                    if (weatherBaseTarget != null && weatherReserveTarget != null && Math.abs(parseFloat(weatherReserveTarget) - parseFloat(weatherBaseTarget)) > 0.2) {
                        msg += ` Speicherziel: ${parseFloat(weatherBaseTarget).toFixed(0)} -> ${parseFloat(weatherReserveTarget).toFixed(0)} %.`;
                    }
                    return msg;
                })();
                const setStableLiveTitle = (el, title) => {
                    if (!el) return;
                    const nextTitle = title || '';
                    if (!el.dataset.liveTitleStable) {
                        el.dataset.liveTitleStable = '1';
                        el.addEventListener('mouseleave', () => {
                            if (el.dataset.pendingTitle !== undefined) {
                                el.title = el.dataset.pendingTitle;
                                delete el.dataset.pendingTitle;
                            }
                        });
                    }
                    if (el.matches(':hover') && el.title && el.title !== nextTitle) {
                        el.dataset.pendingTitle = nextTitle;
                    } else {
                        el.title = nextTitle;
                        delete el.dataset.pendingTitle;
                    }
                };
                if (storageForecastBadge) {
                    if (weatherReserveActive) {
                        storageForecastBadge.style.display = '';
                        storageForecastBadge.textContent = 'Pre-Dump pausiert';
                        storageForecastBadge.style.background = 'rgba(245,158,11,0.15)';
                        storageForecastBadge.style.color = '#f59e0b';
                        setStableLiveTitle(storageForecastBadge, weatherReserveText);
                    } else {
                        storageForecastBadge.style.display = 'none';
                        storageForecastBadge.textContent = '--';
                        setStableLiveTitle(storageForecastBadge, '');
                    }
                }
                if (storStateEl) {
                    if (storageOperational) {
                        storStateEl.textContent = storageOperational.label;
                        storStateEl.style.color = storageOperational.color;
                        setStableLiveTitle(
                            storStateEl,
                            storageOperational.holdActive
                                ? storageOperational.badge
                                : (storageOperational.plannedHint || '')
                        );
                    } else {
                        storStateEl.textContent = data.storage_state_label || data.storage_state || '--';
                        storStateEl.style.color = '#adb5bd';
                    }
                }
                if (storReasonEl && storageOperational && storageOperational.holdActive) {
                    storReasonEl.textContent = storageOperational.detail;
                    storReasonEl.title = storageOperational.badge;
                } else if (storReasonEl && weatherReserveActive) {
                    storReasonEl.textContent = weatherReserveText;
                    storReasonEl.title = storagePlanMeta.weather_reserve_reason || storageCurveMeta.weather_reserve_reason || weatherReserveText;
                } else if (storReasonEl && data.cheap_grid_boost_active && data.cheap_grid_charge && data.cheap_grid_charge.active) {
                    const cg = data.cheap_grid_charge;
                    const target = cg.target_soc != null ? parseFloat(cg.target_soc).toFixed(1) + '%' : '--';
                    const watts = cg.charge_w != null ? Math.round(cg.charge_w) + ' W' : '--';
                    const reserve = cg.future_pv_wh != null ? (parseFloat(cg.future_pv_wh) / 1000).toFixed(1) + ' kWh' : '--';
                    storReasonEl.textContent = `Preis-Boost aktiv: Netzladen bis ${target} mit ${watts}; ${reserve} PV-Freiraum bleibt reserviert.`;
                    storReasonEl.title = data.storage_reason || storReasonEl.textContent;
                } else if (storReasonEl && data.cheap_grid_boost_enabled && data.cheap_grid_boost_next_window && !data.cheap_grid_boost_active) {
                    const w = data.cheap_grid_boost_next_window;
                    const fmtTs = ms => ms ? new Date(parseInt(ms, 10)).toLocaleTimeString('de-DE', {hour:'2-digit', minute:'2-digit'}) : '--';
                    const start = w.start_local || fmtTs(w.start_timestamp);
                    const end = w.end_local || fmtTs(w.end_timestamp);
                    const priceRaw = w.min_price_ct ?? w.min_billing_price ?? w.avg_billing_price;
                    const minPrice = priceRaw != null ? parseFloat(priceRaw).toFixed(2) + ' ct/kWh' : '';
                    storReasonEl.textContent = `Preis-Boost bereit: naechstes Fenster ${start}-${end} ${minPrice}.`;
                    storReasonEl.title = data.storage_reason || storReasonEl.textContent;
                } else if (storReasonEl && data.storage_reason) {
                    const r = data.storage_reason || '--';
                    storReasonEl.textContent = formatStorageReasonInline(r);
                    storReasonEl.title = r;
                }
                if (storSollSocEl) {
                    const eveningReleaseActive = String(data.storage_state || '').toLowerCase() === 'parallel_evening_release';
                    if (eveningReleaseActive) {
                        storSollSocEl.style.display = 'none';
                        storSollSocEl.textContent = 'Soll: -- %';
                    } else {
                        storSollSocEl.style.display = '';
                    // Soll-SoC aus Plan-Meta oder target_soc
                    let sollSoc = null;
                    if (window._storagePlanMeta && window._storagePlanMeta.target_soc != null) {
                        sollSoc = window._storagePlanMeta.target_soc;
                    } else if (data.target_soc != null) {
                        sollSoc = data.target_soc;
                    }
                    storSollSocEl.textContent = sollSoc != null ? ('Soll: ' + parseFloat(sollSoc).toFixed(0) + ' %') : 'Soll: -- %';
                    }
                }
                if (storIfcEl) {
                    const numOrNull = value => value != null && !isNaN(parseFloat(value))
                        ? Math.round(parseFloat(value))
                        : null;
                    const pctOrNull = value => value != null && !isNaN(parseFloat(value))
                        ? parseFloat(value)
                        : null;
                    const kwhText = wh => wh !== null ? (wh / 1000).toFixed(1) + ' kWh' : '-- kWh';
                    const ifcW = numOrNull(data.storage_ifc_w);
                    const iminW = numOrNull(data.storage_imin_w);
                    const chargeReqRawW = numOrNull(data.storage_charge_request_w);
                    const budgetChargeReqW = numOrNull(data.bat_charge_req_w);
                    const chargeReqW = chargeReqRawW !== null ? chargeReqRawW : budgetChargeReqW;
                    const wbCurveReserveW = numOrNull(data.wallbox_curve_reserve_w);
                    const wbCurveReserveTargetW = numOrNull(data.wallbox_curve_reserve_target_w);
                    const wbCurveReserveStepW = numOrNull(data.wallbox_curve_reserve_step_w);
                    const curveNeedRawW = numOrNull(data.storage_curve_need_raw_w);
                    const lookaheadNeedW = numOrNull(data.storage_lookahead_need_w);
                    const curveCatchupW = numOrNull(data.storage_curve_catchup_w);
                    const curveCatchupCapW = numOrNull(data.storage_curve_catchup_cap_w);
                    const curveCatchupMinW = numOrNull(data.storage_curve_catchup_min_w);
                    const curveGapPct = pctOrNull(data.storage_curve_gap_pct);
                    const curveTaperPct = pctOrNull(data.storage_curve_catchup_taper_pct);
                    const curveCatchupFactor = pctOrNull(data.storage_curve_catchup_factor);
                    const abregelW = numOrNull(data.storage_abregel_req_w);
                    const abregelTargetW = numOrNull(data.storage_abregel_target_w);
                    const abregelReleaseW = numOrNull(data.storage_abregel_release_w);
                    const abregelGridErrorW = numOrNull(data.storage_abregel_grid_error_w);
                    const abregelInverterW = numOrNull(data.storage_abregel_inverter_pressure_w);
                    const adaptiveHeadroomRequiredWh = numOrNull(data.storage_adaptive_headroom_required_wh);
                    const adaptiveHeadroomAvailableWh = numOrNull(data.storage_adaptive_headroom_available_wh);
                    const adaptiveHeadroomBufferWh = numOrNull(data.storage_adaptive_headroom_buffer_wh);
                    const adaptiveHeadroomTargetWh = adaptiveHeadroomAvailableWh !== null && adaptiveHeadroomRequiredWh !== null
                        ? adaptiveHeadroomAvailableWh + adaptiveHeadroomRequiredWh
                        : null;
                    const curtailmentPressureWh = numOrNull(data.storage_curtailment_pressure_wh);
                    const curtailmentUnavoidableWh = numOrNull(data.storage_curtailment_unavoidable_wh);
                    const eveningShortfallWh = numOrNull(data.storage_evening_shortfall_wh);
                    const latestChargeStartTs = numOrNull(data.storage_latest_charge_start_ts);
                    const abregelActive = data.storage_abregel_active === true
                        || data.storage_abregel_active === 1
                        || data.storage_abregel_active === '1'
                        || String(data.storage_state || '').toLowerCase() === 'parallel_curve_charge_cap';
                    const showAbregel = abregelActive && abregelW !== null && abregelW > 0;
                    const hasAdaptiveForecastPressure = (curtailmentPressureWh !== null && curtailmentPressureWh > 0)
                        || (adaptiveHeadroomRequiredWh !== null && adaptiveHeadroomRequiredWh > 0)
                        || (eveningShortfallWh !== null && eveningShortfallWh > 0);
                    const inferredChargeReqW = chargeReqW !== null
                        ? chargeReqW
                        : (showAbregel ? abregelW : (ifcW !== null ? ifcW : null));
                    const hasWbCurveReserve = wbCurveReserveW !== null && wbCurveReserveW > 0;
                    const hasChargeRequest = (inferredChargeReqW !== null && inferredChargeReqW > 0) || hasWbCurveReserve;
                    const waitingForReserve = !hasChargeRequest && ifcW !== null && ifcW > 0;
                    if (showAbregel || hasChargeRequest || waitingForReserve || hasAdaptiveForecastPressure) {
                        storIfcEl.style.display = '';
                        const textParts = [];
                        if (showAbregel) textParts.push('Abregel: ' + abregelW + ' W');
                        if (!showAbregel && curtailmentPressureWh !== null && curtailmentPressureWh > 0) {
                            textParts.push('Abregeldruck: ' + kwhText(curtailmentPressureWh));
                        }
                        if (adaptiveHeadroomRequiredWh !== null && adaptiveHeadroomRequiredWh > 0) {
                            textParts.push('Headroom freihalten: ' + kwhText(adaptiveHeadroomRequiredWh));
                        }
                        if (eveningShortfallWh !== null && eveningShortfallWh > 0) {
                            textParts.push('Abendziel: +' + kwhText(eveningShortfallWh));
                        }
                        if (hasWbCurveReserve) {
                            textParts.push('iFc-Führung: ' + wbCurveReserveW + ' W');
                        } else if (hasChargeRequest) {
                            textParts.push('Rahmen: ' + inferredChargeReqW + ' W');
                        } else if (waitingForReserve) {
                            textParts.push('Bedarf wartet: ' + ifcW + ' W');
                        }
                        storIfcEl.textContent = textParts.join(' | ');
                        const emsChargeW = numOrNull(data.ems_max_charge_power_w);
                        const emsDischargeW = numOrNull(data.ems_max_discharge_power_w);
                        const emsLimitsActive = data.power_limits_active === true || data.power_limits_active === 1 || data.power_limits_active === '1';
                        const titleParts = [];
                        if (hasWbCurveReserve) {
                            titleParts.push('Aktive iFc-Führung bleibt beim Speicher: ' + wbCurveReserveW + ' W');
                            if (wbCurveReserveTargetW !== null && wbCurveReserveTargetW > 0) titleParts.push('iFc-Ziel: ' + wbCurveReserveTargetW + ' W');
                            if (wbCurveReserveStepW !== null && wbCurveReserveStepW > 0) titleParts.push('Sanfte Rampe: ' + wbCurveReserveStepW + ' W je Zyklus');
                        } else if (hasChargeRequest) {
                            titleParts.push('Wirksamer Laderahmen: ' + inferredChargeReqW + ' W');
                        } else {
                            titleParts.push('Kein aktiver Laderahmen');
                        }
                        if (showAbregel) {
                            titleParts.push('Abregel-Ladebedarf: ' + abregelW + ' W');
                            if (abregelTargetW !== null) titleParts.push('Abregel-Ziel: ' + abregelTargetW + ' W Einspeisung');
                            if (abregelReleaseW !== null) titleParts.push('Freigabe unter: ' + abregelReleaseW + ' W Einspeisung');
                            if (abregelGridErrorW !== null) titleParts.push('Netzabweichung: ' + abregelGridErrorW + ' W');
                            if (abregelInverterW !== null && abregelInverterW > 0) titleParts.push('PV/WR-Druck: ' + abregelInverterW + ' W (Diagnose, kein Zusatzbefehl)');
                        }
                        if (curtailmentPressureWh !== null) titleParts.push('Abregeldruck über den PV-Tag: ' + kwhText(curtailmentPressureWh));
                        if (curtailmentUnavoidableWh !== null && curtailmentUnavoidableWh > 0) titleParts.push('Nicht durch Speicher vermeidbarer Druck: ' + kwhText(curtailmentUnavoidableWh));
                        if (adaptiveHeadroomAvailableWh !== null) titleParts.push('Bis dahin sicherer Platz: ' + kwhText(adaptiveHeadroomAvailableWh));
                        if (adaptiveHeadroomRequiredWh !== null) titleParts.push('Bis zum Druckfenster freihalten: ' + kwhText(adaptiveHeadroomRequiredWh));
                        if (adaptiveHeadroomBufferWh !== null && adaptiveHeadroomBufferWh > 0) titleParts.push('Regelpuffer: ' + kwhText(adaptiveHeadroomBufferWh));
                        if (eveningShortfallWh !== null && eveningShortfallWh > 0) titleParts.push('Abendziel-Risiko: ' + kwhText(eveningShortfallWh));
                        if (latestChargeStartTs !== null && latestChargeStartTs > 0) {
                            const latestMs = latestChargeStartTs > 10000000000 ? latestChargeStartTs : latestChargeStartTs * 1000;
                            titleParts.push('Spätester Ladestart: ' + new Date(latestMs).toLocaleTimeString('de-DE', {hour:'2-digit', minute:'2-digit'}));
                        }
                        if (data.storage_adaptive_curve_relation) titleParts.push('Korridor-Entscheidung: ' + String(data.storage_adaptive_curve_relation));
                        if (ifcW !== null && ifcW > 0) {
                            titleParts.push('Kurvenbedarf iFc: ' + ifcW + ' W');
                        }
                        if (curveNeedRawW !== null && curveNeedRawW > 0) titleParts.push('Rohbedarf vor Kappe: ' + curveNeedRawW + ' W');
                        if (lookaheadNeedW !== null && lookaheadNeedW > 0) titleParts.push('Lookahead-Bedarf: ' + lookaheadNeedW + ' W');
                        if (curveCatchupW !== null && curveCatchupW > 0) titleParts.push('Aufholbedarf aus Kurvenrueckstand: ' + curveCatchupW + ' W');
                        if (curveGapPct !== null && curveGapPct > 0) titleParts.push('Rückstand zur Sollkurve: ' + curveGapPct.toFixed(1) + ' %');
                        if (curveCatchupCapW !== null && curveCatchupCapW > 0) {
                            let capLine = 'Dynamische Aufhol-Kappe: ' + curveCatchupCapW + ' W';
                            if (curveCatchupMinW !== null && curveCatchupMinW > 0) capLine += ' ab ' + curveCatchupMinW + ' W';
                            if (curveCatchupFactor !== null) capLine += ' (' + Math.round(curveCatchupFactor * 100) + ' %)';
                            if (curveTaperPct !== null && curveTaperPct > 0) capLine += ' im Band ' + curveTaperPct.toFixed(1) + ' %';
                            titleParts.push(capLine);
                        }
                        if (waitingForReserve && data.storage_reason) {
                            titleParts.push('Warum noch nicht geladen wird: ' + formatStorageReasonInline(String(data.storage_reason)));
                        }
                        if (iminW !== null) titleParts.push('iMin: ' + iminW + ' W');
                        if (data.storage_val_w != null) titleParts.push('RSCP-Sollwert: ' + Math.round(parseFloat(data.storage_val_w)) + ' W');
                        if (emsChargeW !== null) titleParts.push('E3DC Max. Laden: ' + emsChargeW + ' W');
                        if (emsDischargeW !== null) titleParts.push('E3DC Max. Entladen: ' + emsDischargeW + ' W');
                        if (data.power_limits_active != null) titleParts.push('E3DC Limits aktiv: ' + (emsLimitsActive ? 'Ja' : 'Nein'));
                        setStableLiveTitle(storIfcEl, titleParts.join('\n'));
                    } else {
                        storIfcEl.style.display = 'none';
                        storIfcEl.textContent = 'Rahmen: -- W';
                        setStableLiveTitle(storIfcEl, '');
                    }
                }
                if (storCurveEl) {
                    const eveningReleaseActive = String(data.storage_state || '').toLowerCase() === 'parallel_evening_release';
                    const curveNow = data.storage_curve_soc_now != null && !isNaN(parseFloat(data.storage_curve_soc_now))
                        ? parseFloat(data.storage_curve_soc_now)
                        : null;
                    const curveTarget = data.storage_curve_soc_target != null && !isNaN(parseFloat(data.storage_curve_soc_target))
                        ? parseFloat(data.storage_curve_soc_target)
                        : null;
                    const curveControlSoc = data.storage_curve_control_soc != null && !isNaN(parseFloat(data.storage_curve_control_soc))
                        ? parseFloat(data.storage_curve_control_soc)
                        : null;
                    const curveRawSoc = data.storage_curve_raw_soc != null && !isNaN(parseFloat(data.storage_curve_raw_soc))
                        ? parseFloat(data.storage_curve_raw_soc)
                        : (data.soc != null && !isNaN(parseFloat(data.soc)) ? parseFloat(data.soc) : null);
                    const adaptiveFloorSoc = data.storage_adaptive_soc_floor != null && !isNaN(parseFloat(data.storage_adaptive_soc_floor))
                        ? parseFloat(data.storage_adaptive_soc_floor)
                        : null;
                    const adaptiveCeilingSoc = data.storage_adaptive_soc_ceiling != null && !isNaN(parseFloat(data.storage_adaptive_soc_ceiling))
                        ? parseFloat(data.storage_adaptive_soc_ceiling)
                        : null;
                    const adaptiveActive = data.storage_adaptive_curve_active === true
                        || data.storage_adaptive_curve_active === 1
                        || data.storage_adaptive_curve_active === '1'
                        || (adaptiveFloorSoc !== null && adaptiveCeilingSoc !== null);
                    if (eveningReleaseActive) {
                        storCurveEl.style.display = 'none';
                        storCurveEl.textContent = 'Kurve: --';
                        storCurveEl.title = 'Freilauf erreicht: keine aktive Kurvenführung mehr';
                    } else if (adaptiveActive && (adaptiveFloorSoc !== null || adaptiveCeilingSoc !== null)) {
                        storCurveEl.style.display = '';
                        const floorTxt = adaptiveFloorSoc !== null ? adaptiveFloorSoc.toFixed(0) + '%' : '--';
                        const ceilingTxt = adaptiveCeilingSoc !== null ? adaptiveCeilingSoc.toFixed(0) + '%' : '--';
                        storCurveEl.textContent = 'Band: ' + floorTxt + '-' + ceilingTxt;
                        const curveTitleParts = ['Adaptiver Zielkorridor: Unterkante verhindert zu spätes Laden, Oberkante hält Platz für PV-Spitzen frei.'];
                        if (adaptiveFloorSoc !== null) curveTitleParts.push('Unterkante: ' + adaptiveFloorSoc.toFixed(1) + '%');
                        if (adaptiveCeilingSoc !== null) curveTitleParts.push('Oberkante: ' + adaptiveCeilingSoc.toFixed(1) + '%');
                        if (curveControlSoc !== null) curveTitleParts.push('Regel-SoC: ' + curveControlSoc.toFixed(1) + '%');
                        if (curveRawSoc !== null && (curveControlSoc === null || Math.abs(curveRawSoc - curveControlSoc) >= 0.25)) {
                            curveTitleParts.push('Live-SoC: ' + curveRawSoc.toFixed(1) + '%');
                        }
                        if (data.storage_adaptive_curve_relation) curveTitleParts.push('Entscheidung: ' + String(data.storage_adaptive_curve_relation));
                        if (data.storage_curtailment_pressure_wh != null) curveTitleParts.push('Abregeldruck über den PV-Tag: ' + (parseFloat(data.storage_curtailment_pressure_wh) / 1000).toFixed(1) + ' kWh');
                        if (data.storage_adaptive_headroom_required_wh != null) {
                            curveTitleParts.push('Bis zum Druckfenster freihalten: ' + (parseFloat(data.storage_adaptive_headroom_required_wh) / 1000).toFixed(1) + ' kWh');
                        }
                        storCurveEl.title = curveTitleParts.join('\n');
                    } else if (curveNow !== null || curveTarget !== null) {
                        storCurveEl.style.display = '';
                        const nowTxt = curveNow !== null ? curveNow.toFixed(1) + '%' : '--';
                        const targetTxt = curveTarget !== null ? curveTarget.toFixed(1) + '%' : '--';
                        storCurveEl.textContent = 'Kurve: ' + nowTxt + ' -> ' + targetTxt;
                        const curveTitleParts = ['Aktueller Kurvenpunkt -> Zwischenziel im Vorausschauhorizont'];
                        if (curveControlSoc !== null) curveTitleParts.push('Regel-SoC: ' + curveControlSoc.toFixed(1) + '%');
                        if (curveRawSoc !== null && (curveControlSoc === null || Math.abs(curveRawSoc - curveControlSoc) >= 0.25)) {
                            curveTitleParts.push('Live-SoC: ' + curveRawSoc.toFixed(1) + '%');
                        }
                        storCurveEl.title = curveTitleParts.join('\n');
                    } else {
                        storCurveEl.style.display = 'none';
                        storCurveEl.textContent = 'Kurve: --';
                        storCurveEl.title = '';
                    }
                }
                if (storageOperational && storageOperational.holdActive) {
                    [storSollSocEl, storIfcEl, storCurveEl].forEach(el => {
                        if (el) el.style.display = 'none';
                    });
                    if (storReasonEl) {
                        storReasonEl.textContent = storageOperational.detail;
                        storReasonEl.title = storageOperational.badge;
                    }
                } else if (
                    storReasonEl
                    && storageOperational
                    && storageOperational.plannedHint
                    && !weatherReserveActive
                    && !data.cheap_grid_boost_active
                ) {
                    storReasonEl.textContent = storageOperational.plannedHint;
                    storReasonEl.title = 'Planung ohne bestätigte Gerätewirkung';
                }
                // Budget-Badge
                const budgetBadge = document.getElementById('wb-budget-badge');
                const budgetStateBadge = document.getElementById('wb-budget-state-badge');
                if (budgetBadge) {
                    const grossW = data.free_for_limbs_w != null ? data.free_for_limbs_w : (data.wb_budget_w != null ? data.wb_budget_w : null);
                    const curveW = data.wb_budget_curve_w != null
                        ? data.wb_budget_curve_w
                        : (grossW != null && data.fuzzy_factor != null ? grossW * parseFloat(data.fuzzy_factor) : null);
                    const effExtraW = data.wb_effective_extra_w != null ? data.wb_effective_extra_w : null;
                    const effLimitW = data.wb_effective_budget_w != null ? data.wb_effective_budget_w : (data.avail_wb_w != null ? data.avail_wb_w : null);
                    const wbCurveReserveW = data.wallbox_curve_reserve_w != null ? parseFloat(data.wallbox_curve_reserve_w) : 0;
                    const hasNativeBudget = effExtraW != null || effLimitW != null || data.fuzzy_factor != null;
                    const storageState = String(data.storage_state || data.wb_budget_storage_state || '');
                    const isPredumpConsumerBudget = storageState === 'pre_discharge_wait' || storageState === 'pre_discharge_consumer_auto';
                    const displayW = isPredumpConsumerBudget
                        ? grossW
                        : (hasNativeBudget
                            ? (curveW != null ? curveW : (effExtraW != null ? effExtraW : effLimitW))
                            : grossW);
                    const capAmp = data.cap_amp != null ? parseInt(data.cap_amp, 10) : 0;
                    budgetBadge.textContent = displayW != null
                        ? ((isPredumpConsumerBudget ? 'Akku frei: ' : (hasNativeBudget ? 'WB-Zusatz: ' : 'Frei: ')) + Math.round(displayW) + ' W')
                        : (isPredumpConsumerBudget ? 'Akku frei: -- W' : (hasNativeBudget ? 'WB-Zusatz: -- W' : 'Frei: -- W'));
                    if (isPredumpConsumerBudget) {
                        const lines = [
                            'Pre-Dump: freigegebene Batterie-Entladeleistung für lokale Verbraucher.',
                            'Verbraucher: ' + ['Wallbox', 'Wärmepumpe', 'Heizstab', 'Klima'].join(', '),
                            grossW != null ? ('Akku-Freigabe: ' + Math.round(grossW) + ' W') : null,
                            'Reale Entladung entsteht nur, soweit nach PV noch Last übrig bleibt.',
                            data.heatpump_budget_w != null ? ('Wärmepumpe-Budget: ' + Math.round(parseFloat(data.heatpump_budget_w)) + ' W') : null,
                            data.wallbox_budget_w != null ? ('Wallbox-Budget: ' + Math.round(parseFloat(data.wallbox_budget_w)) + ' W') : null
                        ].filter(Boolean);
                        setStableLiveTitle(budgetBadge, lines.join('\n'));
                    } else if (hasNativeBudget) {
                        const lines = [
                            'Zusätzliche Wallbox-Freigabe, nicht die aktuelle Ladeleistung.',
                            grossW != null ? ('Brutto Storage: ' + Math.round(grossW) + ' W') : null,
                            curveW != null ? ('Nach Kurvenfaktor: ' + Math.round(curveW) + ' W') : null,
                            wbCurveReserveW > 0 ? ('iFc-Führung Speicher: ' + Math.round(wbCurveReserveW) + ' W') : null,
                            effExtraW != null ? ('Rest nach aktueller WB-Leistung: ' + Math.round(effExtraW) + ' W') : null,
                            effLimitW != null ? ('WB-Deckel gesamt: ' + Math.round(effLimitW) + ' W') : null,
                            data.fuzzy_factor != null ? ('Faktor: ' + parseFloat(data.fuzzy_factor).toFixed(2)) : null,
                            data.fuzzy_delta != null ? ('Kurvenabstand: ' + parseFloat(data.fuzzy_delta).toFixed(1) + ' %') : null,
                            capAmp > 0 ? ('Sollstrom-Deckel: ' + capAmp + ' A') : null,
                            capAmp >= 6 && (displayW == null || displayW <= 0) ? '6A können als ruhige Haltelogik aktiv bleiben, obwohl kein Zusatzbudget frei ist.' : null
                        ].filter(Boolean);
                        setStableLiveTitle(budgetBadge, lines.join('\n'));
                    } else {
                        setStableLiveTitle(budgetBadge, grossW != null ? 'Brutto-Freigabe für lokale Verbraucher aus dem Storage Manager.' : '');
                    }
                    // Farbe nach wirksamer WB-Freigabe, nicht nach Brutto-Storage.
                    if (displayW > 2000) { budgetBadge.style.background='rgba(16,185,129,0.15)'; budgetBadge.style.color='#10b981'; }
                    else if (displayW > 0 || (hasNativeBudget && capAmp >= 6)) { budgetBadge.style.background='rgba(245,158,11,0.15)'; budgetBadge.style.color='#f59e0b'; }
                    else { budgetBadge.style.background='rgba(239,68,68,0.15)'; budgetBadge.style.color='#ef4444'; }
                }
                if (budgetStateBadge) {
                    const bst = data.wb_budget_state || '--';
                    const bstLabels = {run:'Signal OK',reduce:'Gedrosselt',hold:'Halten',stop:'STOP',timeout:'TIMEOUT',unknown:'--'};
                    const bstColors = {run:'#10b981',reduce:'#f59e0b',hold:'#f59e0b',stop:'#ef4444',timeout:'#ef4444',unknown:'#6b7280'};
                    budgetStateBadge.textContent = bstLabels[bst] || bst;
                    budgetStateBadge.style.color = bstColors[bst] || '#adb5bd';
                    const ageS = data.wb_budget_age_s || 0;
                    if (ageS > 10) budgetStateBadge.textContent += ' (' + Math.round(ageS) + 's)';
                }

                // Wärme-Regelung als eigene Manager-Spalte, nicht in der WP-Kachel.
                const heatCol = document.getElementById('heat-manager-col');
                if (heatCol) {
                    const heatConfigured = heatCol.dataset.heatConfigured === '1';
                    if (!heatConfigured) {
                        heatCol.style.setProperty('display', 'none', 'important');
                    } else {
                        const heatStateEl = document.getElementById('heat-manager-state');
                        const heatBadgeEl = document.getElementById('heat-manager-mode-badge');
                        const heatWpModeEl = document.getElementById('heat-manager-wp-mode');
                        const heatBudgetEl = document.getElementById('heat-manager-budget');
                        const budgetW = data.heatpump_budget_w != null && !isNaN(parseFloat(data.heatpump_budget_w))
                            ? Math.max(0, Math.round(parseFloat(data.heatpump_budget_w)))
                            : null;
                        const heatActive = data.heat_manager_active === true
                            || data.wp_boost_active === true
                            || data.wp_pause_active === true
                            || data.wp_market_plan === true
                            || data.wp_price_boost === true
                            || data.wp_predump_boost === true
                            || data.wp_manual_boost === true
                            || data.wp_pre_pause_active === true
                            || String(data.mb_state || '').toUpperCase() === 'RUNNING';
                        let heatLabel = data.heat_manager_label || '';
                        if (!heatLabel || (heatLabel === 'Beobachtet' && budgetW !== null && budgetW > 0)) {
                            if (data.wp_predump_boost === true) heatLabel = 'Pre-Dump';
                            else if (data.wp_pause_active === true || data.wp_pre_pause_active === true) heatLabel = 'Quell-Erholung';
                            else if (data.wp_market_plan === true) heatLabel = 'Marktfenster';
                            else if (data.wp_price_boost === true) heatLabel = 'Preisfenster';
                            else if (data.wp_manual_boost === true) heatLabel = 'Manuell';
                            else if (String(data.mb_state || '').toUpperCase() === 'RUNNING') heatLabel = 'Morgen-Boost';
                            else if (data.wp_boost_active === true) heatLabel = data.heat_manager_owner_label || 'Wärmebudget';
                            else if (budgetW !== null && budgetW > 0) heatLabel = 'Budget bereit';
                            else heatLabel = 'Beobachtet';
                        }
                        const heatReason = data.heat_manager_owner_reason || data.heat_manager_reason || '';
                        const wpModeText = data.wp_mode_text || ({0:'Heizen',1:'WW',5:'Standby'}[data.wp_mode] || 'Standby');
                        const stateColor = heatActive ? '#fb923c' : (budgetW !== null && budgetW > 0 ? '#10b981' : '#adb5bd');
                        if (heatStateEl) {
                            heatStateEl.textContent = heatLabel;
                            heatStateEl.title = heatReason;
                            heatStateEl.style.color = stateColor;
                        }
                        if (heatBadgeEl) {
                            heatBadgeEl.textContent = heatActive ? 'Aktiv' : (budgetW !== null && budgetW > 0 ? 'Budget' : 'Bereit');
                            heatBadgeEl.title = heatReason;
                            heatBadgeEl.style.background = heatActive ? 'rgba(251,146,60,0.16)' : (budgetW !== null && budgetW > 0 ? 'rgba(16,185,129,0.15)' : 'rgba(108,117,125,0.12)');
                            heatBadgeEl.style.color = heatActive ? '#fb923c' : (budgetW !== null && budgetW > 0 ? '#10b981' : '#adb5bd');
                        }
                        if (heatWpModeEl) {
                            heatWpModeEl.textContent = wpModeText;
                            heatWpModeEl.style.color = wpModeText === 'WW' ? '#38bdf8' : (wpModeText === 'Heizen' ? '#fb923c' : '#adb5bd');
                        }
                        if (heatBudgetEl) {
                            heatBudgetEl.textContent = budgetW !== null ? ('Budget: ' + budgetW + ' W') : 'Budget: --';
                            const reason = data.heat_manager_reason || heatReason || data.wp_mode_text || '';
                            heatBudgetEl.title = reason;
                        }
                    }
                }

                // Show the banner if we have storage manager data
                const wbAlert = document.getElementById('wb-native-alert');
                if (wbAlert && data.storage_state) {
                    if (wbAlert.style.display === 'none') {
                        wbAlert.style.display = 'block';
                        wbAlert.style.opacity = 0;
                        setTimeout(() => { wbAlert.style.transition = 'opacity 0.5s'; wbAlert.style.opacity = 1; }, 50);
                    }
                }
            })();

            if (data.ladekurve) {
                const lk = data.ladekurve;
                const dayLabel = (lk.day_label || (window._storagePlanMeta && window._storagePlanMeta.display_day_label) || 'Heute');
                const isFutureDay = dayLabel === 'Morgen' || (lk.day_offset || 0) > 0;
                const fmtSoc = v => (v !== null && v !== undefined && !isNaN(parseFloat(v))) ? parseFloat(v).toFixed(1) + '%' : '--';
                $('#stat-regler-day, #sc-modal-day').text(dayLabel);
                $('#stat-regler-rb-label').text(isFutureDay ? 'Morgenpuffer:' : 'Kurvenstart:');
                $('#stat-regler-soll-label').text(isFutureDay ? 'Morgenpuffer' : 'Jetzt');

                // Phase-Badge
                if (data.storage_phase) {
                    $('#stat-storage-phase, #sc-modal-phase').text(data.storage_phase);
                }

                // Ladestart
                const lsT   = lk.ladestart ? lk.ladestart.t   : '--:--';
                const lsSoc = lk.ladestart ? lk.ladestart.soc : null;
                $('#stat-regler-rb-time').text(lsT);
                const startSuffix = isFutureDay
                    ? (!lk.has_target_curve ? ' (Prognose)' : ' (Puffer)')
                    : ' (Soll)';
                $('#stat-regler-rb-soc').text(lsSoc !== null ? (fmtSoc(lsSoc) + startSuffix) : '');

                // PV-Peak – abgeblendet wenn heute bereits vergangen
                const pkT    = lk.peak ? lk.peak.t    : '--:--';
                const pkSoc  = lk.peak ? lk.peak.soc  : null;
                const pkKw   = lk.peak ? lk.peak.pv_kw : null;
                const pkPast = lk.peak ? !!lk.peak.past : false;
                const pkSource = lk.peak ? String(lk.peak.source || '') : '';
                const peakIsLive = pkSource === 'live_history';
                const hasRealPeak = pkKw !== null && pkKw >= 0.5;
                let peakLabel = hasRealPeak ? pkT : 'keine PV-Spitze';
                if (hasRealPeak) peakLabel += ' ~' + parseFloat(pkKw).toFixed(1) + ' kW';
                $('#stat-regler-peak-title').text(peakIsLive ? 'PV-Spitze bisher:' : 'PV-Höchstleistung:');
                $('#stat-regler-re-time')
                    .text(peakLabel)
                    .css('opacity', pkPast ? '0.45' : '1');
                $('#stat-regler-re-soc')
                    .text(pkPast ? (peakIsLive ? '(bisher)' : '(war)') : (pkSoc !== null ? fmtSoc(pkSoc) : ''))
                    .css('opacity', pkPast ? '0.45' : '1');

                // Freilauf – immer echte Uhrzeit
                const frT   = lk.freilauf ? lk.freilauf.t   : '--:--';
                const frSoc = lk.freilauf ? lk.freilauf.soc : null;
                $('#stat-regler-le-time').text(frT);
                $('#stat-regler-le-soc').text(frSoc !== null ? fmtSoc(frSoc) : '');

                // Header-Info (kompakt im Energiefluss, nur XL-Desktop)
                const headerNowMs = Date.now();
                const interpHeaderCurveSoc = (curve, ts) => {
                    if (!Array.isArray(curve) || curve.length === 0) return null;
                    if (ts <= curve[0].ts) return curve[0].soc;
                    if (ts >= curve[curve.length - 1].ts) return curve[curve.length - 1].soc;
                    for (let i = 0; i < curve.length - 1; i++) {
                        if (ts >= curve[i].ts && ts <= curve[i + 1].ts) {
                            const duration = Math.max(1, curve[i + 1].ts - curve[i].ts);
                            const frac = (ts - curve[i].ts) / duration;
                            return curve[i].soc + (curve[i + 1].soc - curve[i].soc) * frac;
                        }
                    }
                    return null;
                };
                const headerControlSoc = data.storage_curve_control_soc != null && !isNaN(parseFloat(data.storage_curve_control_soc))
                    ? parseFloat(data.storage_curve_control_soc)
                    : null;
                const headerRawSoc = data.soc != null && !isNaN(parseFloat(data.soc)) ? parseFloat(data.soc) : null;
                const headerSoc = headerControlSoc !== null ? headerControlSoc : headerRawSoc;
                const headerFloorNow = interpHeaderCurveSoc(window._storageSocMinCurve || [], headerNowMs);
                const headerTargetNow = interpHeaderCurveSoc(window._storageSollCurve || [], headerNowMs);
                const headerCeilingNow = interpHeaderCurveSoc(window._storageSocCeilingCurve || [], headerNowMs);
                let headerTargetLabel = '--';
                let headerTargetTitle = 'Aktives Regelziel';
                let headerTargetIcon = 'fas fa-bullseye text-info opacity-75';
                if (headerSoc !== null && headerFloorNow !== null && headerSoc < headerFloorNow - 0.3) {
                    headerTargetLabel = 'Unterkante ' + headerFloorNow.toFixed(0) + '%';
                    headerTargetTitle = 'Aktives Regelziel: Speicher liegt unter der Zielkorridor-Unterkante.\nRegel-SoC: ' + headerSoc.toFixed(1) + '%\nUnterkante jetzt: ' + headerFloorNow.toFixed(1) + '%\nDer Tagespfad bleibt der Rahmen; im Ladekurven-Detail ist die Sollkurve sichtbar.';
                    headerTargetIcon = 'fas fa-bullseye text-success opacity-75';
                } else if (headerSoc !== null && headerCeilingNow !== null && headerSoc > headerCeilingNow + 0.3) {
                    headerTargetLabel = 'Oberkante ' + headerCeilingNow.toFixed(0) + '%';
                    headerTargetTitle = 'Aktives Regelziel: Speicher liegt über der Zielkorridor-Oberkante.\nRegel-SoC: ' + headerSoc.toFixed(1) + '%\nOberkante jetzt: ' + headerCeilingNow.toFixed(1) + '%';
                    headerTargetIcon = 'fas fa-bullseye text-warning opacity-75';
                } else if (headerTargetNow !== null) {
                    headerTargetLabel = 'Sollkurve ' + headerTargetNow.toFixed(0) + '%';
                    headerTargetTitle = 'Aktives Regelziel: Sollkurve im Zielkorridor.\n' + (headerSoc !== null ? 'Regel-SoC: ' + headerSoc.toFixed(1) + '%\n' : '') + 'Sollkurve jetzt: ' + headerTargetNow.toFixed(1) + '%';
                    headerTargetIcon = 'fas fa-route text-info opacity-75';
                } else if (hasRealPeak) {
                    headerTargetLabel = pkT + (pkSoc !== null && !pkPast ? ' (' + parseFloat(pkSoc).toFixed(0) + '%)' : '');
                    headerTargetTitle = peakIsLive ? 'PV-Spitze bisher' : 'PV-Höchstleistung laut Prognose';
                    headerTargetIcon = 'fas fa-stop text-warning opacity-75';
                }
                $('#header-regler-plan').removeClass('d-none').addClass('d-none d-xl-flex');
                $('#head-rb').text(dayLabel + ' ' + lsT + (lsSoc !== null && lsT !== 'Aktiv' ? ' (' + parseFloat(lsSoc).toFixed(0) + '%)' : ''));
                $('#head-re').text(headerTargetLabel);
                $('#head-re-wrap').attr('title', headerTargetTitle);
                $('#head-re-icon').attr('class', headerTargetIcon);
                $('#head-le').text(frT + (frSoc !== null ? ' (' + parseFloat(frSoc).toFixed(0) + '%)' : ''));

                // Soll-SoC jetzt (interpoliert) + Meta in der Kachel-Fusszeile
                const nowMs = headerNowMs;
                const sollCurve = window._storageSollCurve || [];
                let sollNow = null;
                for (let i = 0; i < sollCurve.length - 1; i++) {
                    if (nowMs >= sollCurve[i].ts && nowMs <= sollCurve[i+1].ts) {
                        const frac = (nowMs - sollCurve[i].ts) / (sollCurve[i+1].ts - sollCurve[i].ts);
                        sollNow = sollCurve[i].soc + (sollCurve[i+1].soc - sollCurve[i].soc) * frac;
                        break;
                    }
                }
                if (sollNow === null && sollCurve.length > 0) {
                    if (nowMs < sollCurve[0].ts) sollNow = sollCurve[0].soc;
                    else sollNow = sollCurve[sollCurve.length-1].soc;
                }
                if (sollNow !== null) $('#stat-regler-soll-now').text(fmtSoc(sollNow));
                else if (isFutureDay && lsSoc !== null) $('#stat-regler-soll-now').text(fmtSoc(lsSoc));

                const emsChargeRaw = data.ems_max_charge_power_w != null ? data.ems_max_charge_power_w : data.storage_max_charge_w;
                const emsDischargeRaw = data.ems_max_discharge_power_w != null ? data.ems_max_discharge_power_w : data.storage_max_discharge_w;
                const emsChargeText = fmtPowerShort(emsChargeRaw);
                const emsDischargeText = fmtPowerShort(emsDischargeRaw);
                const emsRead = data.ems_power_settings_read === true || data.ems_power_settings_read === 1 || data.ems_power_settings_read === '1';
                const emsLimitsKnown = data.power_limits_active !== undefined && data.power_limits_active !== null;
                const emsLimitsActive = data.power_limits_active === true || data.power_limits_active === 1 || data.power_limits_active === '1';
                const emsTitle = 'E3DC Power-Settings'
                    + '\nMax. Laden: ' + emsChargeText
                    + '\nMax. Entladen: ' + emsDischargeText
                    + (emsLimitsKnown ? '\nEMS-Grenzen aktiv: ' + (emsLimitsActive ? 'Ja' : 'Nein') : '')
                    + (emsRead ? '\nQuelle: RSCP live' : '\nQuelle: Manager/Fallback');
                $('#stat-ems-max-charge').text(emsChargeText);
                $('#stat-ems-max-discharge').text(emsDischargeText);
                $('#stat-ems-limits-state')
                    .removeClass('text-muted text-success text-warning')
                    .addClass(emsLimitsKnown ? (emsLimitsActive ? 'text-warning' : 'text-success') : 'text-muted')
                    .text(emsLimitsKnown ? (emsLimitsActive ? 'EMS-Grenze aktiv' : 'EMS frei') : (emsRead ? 'EMS gelesen' : 'EMS --'))
                    .attr('title', emsTitle);
                $('#stat-ems-max-charge, #stat-ems-max-discharge').parent().attr('title', emsTitle);

                let curveRiseT = null;
                if (lsSoc !== null && sollCurve.length > 0) {
                    const baseSoc = parseFloat(lsSoc);
                    const dayStart = lk.day_start_ts != null ? parseFloat(lk.day_start_ts) : null;
                    const dayEnd = dayStart != null ? dayStart + 86400000 : null;
                    const riseSlot = sollCurve
                        .map(s => ({ ts: parseFloat(s.ts), soc: parseFloat(s.soc) }))
                        .filter(s => Number.isFinite(s.ts) && Number.isFinite(s.soc))
                        .filter(s => dayStart == null || (s.ts >= dayStart && s.ts < dayEnd))
                        .sort((a, b) => a.ts - b.ts)
                        .find(s => s.soc > baseSoc + 0.3);
                    if (riseSlot) {
                        curveRiseT = new Date(riseSlot.ts).toLocaleTimeString('de-DE', {hour:'2-digit', minute:'2-digit'});
                    }
                }

                if (window._storagePlanMeta) {
                    const m = window._storagePlanMeta;
                    let metaStr = '';
                    if (m.q_ratio != null) metaStr = 'Q=' + parseFloat(m.q_ratio).toFixed(1);
                    if (m.bat_cap_kwh != null) metaStr += ' | ' + parseFloat(m.bat_cap_kwh).toFixed(1) + ' kWh';
                    $('#stat-regler-meta').text(metaStr);
                }

                let summary = '';
                if (isFutureDay) {
                    summary = curveRiseT
                        ? `Morgenpuffer ist die Untergrenze; Kurvenanstieg ab ${curveRiseT}.`
                        : 'Morgenpuffer ist die Untergrenze; die echte Ladekurve folgt dem PV-Überschuss.';
                } else if (hasRealPeak) {
                    summary = 'Die Ladekurve führt den Speicher ruhig zum Freilauf-SoC und gibt danach frei.';
                } else {
                    summary = 'Heute ist keine relevante PV-Spitze mehr geplant.';
                }
                if (window._storagePlanMeta && window._storagePlanMeta.has_target_curve === false) {
                    summary += ' Der Tagespfad wird beim nächsten Tagesplan neu eingefroren.';
                }
                $('#stat-regler-summary').text(summary);

                $('#card-regler-wrapper').show();

            } else if (data.storage_plan_meta && data.storage_plan_meta.clear_classical_curves === true) {
                $('#stat-regler-rb-time, #stat-regler-re-time, #stat-regler-le-time').text('--:--');
                $('#stat-regler-rb-soc, #stat-regler-re-soc, #stat-regler-le-soc').text('');
                $('#stat-regler-soll-now, #stat-regler-meta').text('--');
                $('#stat-regler-summary').text('Die Direktvermarktung führt den aktuellen Slot.');
                $('#head-rb, #head-re, #head-le').text('--');
                $('#card-regler-wrapper').hide();
                $('#header-regler-plan').removeClass('d-xl-flex text-muted').addClass('d-none');
            } else if (data.regler && data.regler.rb_time) {
                // Legacy-Fallback: C++ Fahrplan (alt)
                const toLocal = gmtTime => {
                    if(!gmtTime) return '--:--';
                    let p = gmtTime.split(':');
                    if(p.length !== 2) return gmtTime;
                    let d = new Date();
                    d.setUTCHours(parseInt(p[0], 10), parseInt(p[1], 10), 0, 0);
                    return d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');
                };
                let rbLocal = toLocal(data.regler.rb_time);
                let reLocal = toLocal(data.regler.re_time);
                let leLocal = toLocal(data.regler.le_time);
                $('#stat-regler-rb-time').text(rbLocal);
                $('#stat-regler-rb-soc').text(parseFloat(data.regler.rb_soc).toFixed(1) + '%');
                $('#stat-regler-re-time').text(reLocal);
                $('#stat-regler-re-soc').text(parseFloat(data.regler.re_soc).toFixed(1) + '%');
                $('#stat-regler-le-time').text(leLocal);
                $('#stat-regler-le-soc').text(parseFloat(data.regler.le_soc).toFixed(1) + '%');
                $('#header-regler-plan').removeClass('d-none').addClass('d-none d-xl-flex');
                $('#head-rb').text(rbLocal + ' (' + parseFloat(data.regler.rb_soc).toFixed(0) + '%)');
                $('#head-re').text(reLocal + ' (' + parseFloat(data.regler.re_soc).toFixed(0) + '%)');
                $('#head-re-wrap').attr('title', 'Regelende');
                $('#head-re-icon').attr('class', 'fas fa-stop text-warning opacity-75');
                $('#head-le').text(leLocal + ' (' + parseFloat(data.regler.le_soc).toFixed(0) + '%)');
                // $('#card-regler-wrapper').show();  // Legacy: nur Header zeigen
            } else {
                $('#card-regler-wrapper').hide();
                $('#header-regler-plan').removeClass('d-xl-flex text-muted').addClass('d-none');
            }
        }

        let desktopLiveFetchPromise = null;
        let desktopLiveFetchController = null;
        let desktopLiveRequestGeneration = 0;
        const desktopLiveFetchTimeoutMs = 10000;

        function invalidateDesktopLiveFetch() {
            desktopLiveRequestGeneration += 1;
            if (desktopLiveFetchController) desktopLiveFetchController.abort();
            desktopLiveFetchController = null;
            desktopLiveFetchPromise = null;
        }

        function fetchData() {
            if (typeof e3dcLiveAuthBlocked === 'function' && e3dcLiveAuthBlocked()) return Promise.resolve(null);
            const wsFresh = window.liveWs
                && window.liveWs.readyState === WebSocket.OPEN
                && window.liveWsLastMessageTs
                && (Date.now() - window.liveWsLastMessageTs) < 5000;
            if (wsFresh) return Promise.resolve(null); // WebSocket nur bevorzugen, wenn er wirklich frische Daten liefert
            if (desktopLiveFetchPromise) return desktopLiveFetchPromise;

            const requestGeneration = ++desktopLiveRequestGeneration;
            const controller = typeof AbortController === 'function' ? new AbortController() : null;
            desktopLiveFetchController = controller;
            let timeoutId = null;
            const timeoutPromise = new Promise(function(_resolve, reject) {
                timeoutId = setTimeout(function() {
                    if (controller) controller.abort();
                    const error = new Error('Live-Anfrage überschritt das Zeitlimit');
                    error.name = 'AbortError';
                    reject(error);
                }, desktopLiveFetchTimeoutMs);
            });
            const requestPromise = e3dcFetchLiveJson(
                'get_live_json.php?t=' + Date.now(),
                controller ? {signal: controller.signal} : {}
            ).then(function(response) {
                if (typeof e3dcReadLiveJsonResponse === 'function') return e3dcReadLiveJsonResponse(response);
                if (!response.ok) throw new Error('HTTP ' + response.status);
                return response.json();
            });
            const trackedPromise = Promise.race([requestPromise, timeoutPromise]).then(function(data) {
                if (requestGeneration !== desktopLiveRequestGeneration) return;
                if (typeof e3dcClearLiveAuthRecovery === 'function') e3dcClearLiveAuthRecovery();
                processLiveData(data);
                updatePeakShaving(data);
            }).catch(function(error) {
                if (requestGeneration !== desktopLiveRequestGeneration) return;
                if (error && error.name === 'AbortError') return;
                if (typeof e3dcHandleLiveAuthFailure === 'function' && e3dcHandleLiveAuthFailure(error)) return;
                $('#connection-status').removeClass('bg-secondary bg-success').addClass('bg-danger').text('Offline');
            }).finally(function() {
                if (timeoutId !== null) clearTimeout(timeoutId);
                if (desktopLiveFetchPromise === trackedPromise) desktopLiveFetchPromise = null;
                if (desktopLiveFetchController === controller) desktopLiveFetchController = null;
            });
            desktopLiveFetchPromise = trackedPromise;
            return desktopLiveFetchPromise;
        }

        let livePollTimer = null;
        let livePollGeneration = 0;
        let desktopLiveTransportStarted = false;
        let desktopLiveLastResumeMs = 0;
        const desktopWebSocketEnabled = false;
        function livePollDelayMs() {
            return document.hidden ? 10000 : 2000;
        }
        function scheduleLivePoll(immediate = false) {
            const generation = ++livePollGeneration;
            if (livePollTimer) clearTimeout(livePollTimer);
            function tickLivePoll() {
                Promise.resolve(fetchData()).finally(function() {
                    if (generation !== livePollGeneration) return;
                    livePollTimer = setTimeout(tickLivePoll, livePollDelayMs());
                });
            }
            if (immediate) {
                tickLivePoll();
            } else {
                livePollTimer = setTimeout(tickLivePoll, livePollDelayMs());
            }
        }
        function resumeDesktopLiveTransport() {
            if (document.hidden) return false;
            if (!desktopLiveTransportStarted) return startDesktopLiveTransportOnce();
            const now = Date.now();
            if ((now - desktopLiveLastResumeMs) < 500) return true;
            desktopLiveLastResumeMs = now;
            invalidateDesktopLiveFetch();
            scheduleLivePoll(true);
            return true;
        }
        document.addEventListener('visibilitychange', function() {
            if (!desktopLiveTransportStarted) return;
            if (document.hidden) scheduleLivePoll(false);
            else resumeDesktopLiveTransport();
        });
        window.addEventListener('pageshow', resumeDesktopLiveTransport);
        window.addEventListener('focus', resumeDesktopLiveTransport);
        window.addEventListener('online', resumeDesktopLiveTransport);

        function initWebSocket() {
            const protocol = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
            window.liveWs = new WebSocket(protocol + window.location.host + '/ws');
            window.liveWs.onmessage = function(e) {
                const data = JSON.parse(e.data);
                window.liveWsLastMessageTs = Date.now();
                processLiveData(data);
                updatePeakShaving(data);
            };
            window.liveWs.onclose = function() { window.liveWsLastMessageTs = 0; setTimeout(initWebSocket, 3000); }; // Auto-Reconnect
            window.liveWs.onerror = function() { window.liveWsLastMessageTs = 0; window.liveWs.close(); };
        }

        // updateChart, updateChartHistory, switchChartMode, loadArchive, refreshData entfernt -> jetzt in solar.js

        // startSystemUpdate(), pollUpdate() entfernt -> jetzt in solar.js (nutze Button ID 'btn-update-config')

        // Solar wird mit defer geladen. Erst nach dessen vollständiger Ausführung darf
        // der erste Live-Transport Daten an processLiveData übergeben.
        function startDesktopLiveTransportOnce() {
            if (desktopLiveTransportStarted) return true;
            if (typeof window.processLiveData !== 'function') return false;
            desktopLiveTransportStarted = true;
            desktopLiveLastResumeMs = Date.now();
            // Der WebSocket besitzt inzwischen einen nativen read-only
            // Snapshot-Producer. Er bleibt im Browser dennoch deaktiviert,
            // bis auch Handshake und Reconnect denselben Web-Auth-Vertrag wie
            // der POST-Pollingpfad nachweislich erfüllen.
            if (desktopWebSocketEnabled) initWebSocket();
            scheduleLivePoll(true);
            return true;
        }
        window.addEventListener('e3dc:solar-ready', startDesktopLiveTransportOnce, {once: true});
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', startDesktopLiveTransportOnce, {once: true});
        } else {
            startDesktopLiveTransportOnce();
        }

        // Diagramm beim Laden aktualisieren, um veraltete Daten/falschen Modus zu vermeiden
        $(document).ready(function() {
            if ('<?= $seite ?>' === 'dashboard') {
                switchChartMode('flow');
                refreshData(true);
            }
        });




        // Watchdog Status prüfen
        function checkWatchdog() {
            $.getJSON('index.php?action=watchdog_status', function(data) {
                const badge = $('#watchdog-badge');
                if (data.installed) {
                    badge.show();
                    badge.attr('title', data.message);
                    if (data.warning) {
                        badge.removeClass('bg-secondary bg-success bg-danger').addClass('bg-warning text-dark');
                    } else if (data.active) {
                        badge.removeClass('bg-secondary bg-danger bg-warning text-dark').addClass('bg-success text-body');
                    } else {
                        badge.removeClass('bg-secondary bg-success bg-warning text-dark').addClass('bg-danger text-body');
                    }
                } else {
                    badge.hide();
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
            });
        }
        setInterval(checkWatchdog, 10000); // Alle 10 Sek prüfen
        checkWatchdog();

        // showWatchdogLog() entfernt -> jetzt in solar.js

        // handleConnectionClick() entfernt -> jetzt in solar.js

        // --- Interaktive Diagramm-Logik für Tagesstatistik ---
        let chartMix, chartAutarky, chartSelfcon;

        function initStatsCharts() {
            if (chartMix) return; // Bereits initialisiert

            Chart.defaults.color = DARK_MODE ? '#aaa' : '#666';
            Chart.defaults.font.family = "'Segoe UI', Tahoma, Geneva, Verdana, sans-serif";

            const mixCtx = document.getElementById('chartMix').getContext('2d');
            chartMix = new Chart(mixCtx, {
                type: 'doughnut',
                data: {
                    labels: ['Sonne (PV)', 'Batterie', 'Netzbezug'],
                    datasets: [{
                        data: [0, 0, 0],
                        backgroundColor: ['#ffc107', '#198754', '#dc3545'],
                        borderWidth: 2,
                        borderColor: DARK_MODE ? '#1e1e1e' : '#ffffff',
                        hoverOffset: 10
                    }]
                },
                options: {
                    responsive: true, maintainAspectRatio: false, cutout: '55%',
                    plugins: {
                        legend: { display: false },
                        tooltip: { callbacks: { label: (ctx) => ` ${ctx.label}: ${ctx.raw} kWh` } }
                    },
                    onClick: (event, elements) => {
                        if (elements.length > 0) {
                            showDetailCard(elements[0].index);
                        }
                    },
                    onHover: (event, chartElement) => {
                        event.native.target.style.cursor = chartElement[0] ? 'pointer' : 'default';
                    }
                }
            });

            const gridColor = DARK_MODE ? '#333' : '#e9ecef';
            const borderColor = DARK_MODE ? '#1e1e1e' : '#ffffff';

            const autarkyCtx = document.getElementById('chartAutarky').getContext('2d');
            chartAutarky = new Chart(autarkyCtx, {
                type: 'doughnut',
                data: { labels: ['Autark', 'Netz'], datasets: [{ data: [0, 100], backgroundColor: ['#198754', gridColor], borderWidth: 2, borderColor: borderColor }] },
                options: { responsive: true, maintainAspectRatio: false, cutout: '75%', plugins: { legend: { display: false }, tooltip: { enabled: false } }, events: [] }
            });

            const selfconCtx = document.getElementById('chartSelfcon').getContext('2d');
            chartSelfcon = new Chart(selfconCtx, {
                type: 'doughnut',
                data: { labels: ['Eigenverbrauch', 'Einspeisung'], datasets: [{ data: [0, 100], backgroundColor: ['#ffc107', gridColor], borderWidth: 2, borderColor: borderColor }] },
                options: { responsive: true, maintainAspectRatio: false, cutout: '75%', plugins: { legend: { display: false }, tooltip: { enabled: false } }, events: [] }
            });
        }

        function updateInteractiveCharts() {
            initStatsCharts(); // Sicherstellen, dass sie existieren

            const parseVal = (id) => {
                const el = document.getElementById(id);
                if (!el) return 0;
                const match = el.innerText.match(/[\d,.]+/);
                return match ? parseFloat(match[0].replace(',', '.')) : 0;
            };

            const pv = parseVal('stat-pv-total');
            const bat = parseVal('stat-bat-total');
            const grid = parseVal('stat-grid-total');
            const autarky = parseVal('stat-overlay-autarky');
            const selfcon = parseVal('stat-overlay-selfcon');

            chartMix.data.datasets[0].data = [pv, bat, grid];

            const gridColor = DARK_MODE ? '#333' : '#e9ecef';
            const borderColor = DARK_MODE ? '#1e1e1e' : '#ffffff';

            chartMix.data.datasets[0].borderColor = borderColor;
            chartMix.update();

            // Legende aktualisieren (5 Werte)
            const feedin = parseVal('stat-grid-out-total');
            const batIn = parseVal('stat-bat-in-total');
            const setT = (id, v) => { const e = document.getElementById(id); if (e) e.innerText = v.toFixed(1); };
            setT('stat-mix-pv', pv); setT('stat-mix-bat', bat); setT('stat-mix-grid', grid);
            setT('stat-mix-feedin', feedin); setT('stat-mix-bat-in', batIn);

            // CO2-Fußabdruck: Eigenverbrauch (PV-Einspeisung + Bat-Entladung) * 0.38 kg/kWh (DE Strommix)
            const CO2_FACTOR = 0.38; // kg CO2 pro kWh Netzstrom (Deutschland 2024)
            const pvSelfConsumed = Math.max(0, pv - feedin);
            const co2Saved = (pvSelfConsumed + bat) * CO2_FACTOR;
            const co2El = document.getElementById('stat-co2-value');
            if (co2El) co2El.innerText = co2Saved.toFixed(1);

            // 🌱 Baum wächst mit Autarkiegrad
            const treeEl = document.getElementById('co2-tree');
            if (treeEl) {
                let tree, size;
                if (autarky >= 95)      { tree = '🌲🌳🌲'; size = '2.2rem'; }
                else if (autarky >= 80) { tree = '🌲🌳';   size = '2.4rem'; }
                else if (autarky >= 60) { tree = '🌳';      size = '2.8rem'; }
                else if (autarky >= 40) { tree = '🪴';      size = '2.5rem'; }
                else if (autarky >= 20) { tree = '🌿';      size = '2.5rem'; }
                else                    { tree = '🌱';      size = '2.5rem'; }
                treeEl.innerText = tree;
                treeEl.style.fontSize = size;
                treeEl.title = `Autarkie ${autarky}% – ${co2Saved.toFixed(1)} kg CO₂ gespart`;
            }

            chartAutarky.data.datasets[0].data = [autarky, 100 - autarky];
            chartAutarky.data.datasets[0].backgroundColor[1] = gridColor;
            chartAutarky.data.datasets[0].borderColor = borderColor;
            chartAutarky.update();

            chartSelfcon.data.datasets[0].data = [selfcon, 100 - selfcon];
            chartSelfcon.data.datasets[0].backgroundColor[1] = gridColor;
            chartSelfcon.data.datasets[0].borderColor = borderColor;
            chartSelfcon.update();
        }

        function showDetailCard(index) {
            document.getElementById('detail-card-pv').style.display = (index === 0) ? 'block' : 'none';
            document.getElementById('detail-card-bat').style.display = (index === 1) ? 'block' : 'none';
            document.getElementById('detail-card-grid').style.display = (index === 2) ? 'block' : 'none';
            // Kosten-Karte bleibt immer sichtbar (ergänzende Bilanz)
        }

        // Magie: Dieser Observer bemerkt automatisch, wenn solar.js die Tabellenwerte ändert oder einblendet, und baut sofort das Diagramm neu.
        window.addEventListener('DOMContentLoaded', () => {
            const statsView = document.getElementById('stats-view');
            if (statsView) {
                new MutationObserver(() => {
                    if (statsView.style.display !== 'none') setTimeout(updateInteractiveCharts, 50);
                }).observe(statsView, { attributes: true, attributeFilter: ['style'] });

                const dataNode = document.getElementById('stat-pv-total');
                if (dataNode) {
                    new MutationObserver(() => {
                        if (statsView.style.display !== 'none') updateInteractiveCharts();
                    }).observe(dataNode, { childList: true, characterData: true, subtree: true });
                }
            }
        });

        window.addEventListener('themeChanged', () => {
            if (chartMix) {
                updateInteractiveCharts();
            }
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
    <script>
        // Sticky Header: Schatten wird beim Scrollen aktiviert
        (function() {
            const nav = document.querySelector('.navbar');
            if (!nav) return;
            window.addEventListener('scroll', function() {
                nav.classList.toggle('scrolled', window.scrollY > 4);
            }, { passive: true });
        })();

        // Native Wallbox Updater (Isolated)
        let nativeWallboxDisplayCache = null;
        const nativeWallboxHoldMs = 18000;

        function escapeHtmlText(value) {
            return String(value ?? '').replace(/[&<>"']/g, function(ch) {
                return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[ch]);
            });
        }

        function nativeWallboxControlInfo(data, details) {
            const items = [];
            if (Array.isArray(details)) {
                details.forEach(wb => {
                    if (wb && wb.control_label) items.push(wb);
                });
            }
            if (items.length === 0 && data && data.wb_control_label) {
                items.push({
                    control_status: data.wb_control_status,
                    control_label: data.wb_control_label,
                    control_detail: data.wb_control_detail,
                    control_level: data.wb_control_level
                });
            }
            if (items.length === 0) return {label: '', detail: '', className: 'text-muted'};
            const priority = {
                set_current_failed: 10,
                stop_accepted: 8,
                set_current_accepted: 7,
                primary_current_accepted: 6,
                primary_mode_accepted: 5,
                self_regulated: 3
            };
            const selected = items.slice().sort((a, b) => {
                return (priority[String(b.control_status || '')] || 1) - (priority[String(a.control_status || '')] || 1);
            })[0];
            const level = String(selected.control_level || 'info');
            const classMap = {
                success: 'text-success',
                warning: 'text-warning',
                danger: 'text-danger',
                secondary: 'text-muted',
                info: 'text-info'
            };
            return {
                label: String(selected.control_label || ''),
                detail: String(selected.control_detail || selected.control_label || ''),
                className: classMap[level] || 'text-info'
            };
        }

        function nativeWallboxRscpInfo(data, details) {
            const items = [];
            if (Array.isArray(details)) {
                details.forEach(wb => {
                    if (wb && (wb.rscp_error_active === true || String(wb.rscp_status || '').toLowerCase() === 'error')) {
                        items.push(wb);
                    }
                });
            }
            if (items.length === 0 && data && (data.rscp_error_active === true || String(data.rscp_status || '').toLowerCase() === 'error')) {
                items.push(data);
            }
            if (items.length === 0) return {label: '', detail: '', className: 'text-muted'};
            const selected = items.slice().sort((a, b) => {
                return (parseInt(b.rscp_last_error_ts || 0, 10) || 0) - (parseInt(a.rscp_last_error_ts || 0, 10) || 0);
            })[0];
            const ts = parseInt(selected.rscp_last_error_ts || 0, 10) || 0;
            const timeText = ts > 0 ? new Date(ts * 1000).toLocaleTimeString('de-DE') : '';
            const wbText = selected.id ? ('WB' + selected.id + ': ') : '';
            const error = String(selected.rscp_last_error || 'RSCP-Zugriff fehlgeschlagen');
            const context = String(selected.rscp_last_error_context || '');
            const count = parseInt(selected.rscp_error_count || 0, 10) || 0;
            const detailParts = [
                wbText + error,
                context ? ('Kontext: ' + context) : '',
                timeText ? ('Zeit: ' + timeText) : '',
                count > 0 ? ('Fehlerzähler: ' + count) : ''
            ].filter(Boolean);
            return {
                label: 'RSCP Fehler',
                detail: detailParts.join('\n'),
                className: 'text-danger'
            };
        }

        function nativeWallboxE3dcInfo(details) {
            if (!Array.isArray(details)) return {label: '', familyLabel: '', detail: '', className: 'text-muted'};
            const wb = details.find(item => item && item.e3dc_transport === 'e3dc_rscp_via_home_power_station');
            if (!wb) return {label: '', familyLabel: '', detail: '', className: 'text-muted'};
            const familyLabels = {
                efy: 'E3/DC Wallbox efy',
                easy_connect: 'E3/DC Easy Connect',
                multi_connect: 'E3/DC Multi Connect',
                multi_connect_ii: 'E3/DC Multi Connect II',
                unknown: 'E3/DC Wallbox'
            };
            const backendLabels = {
                wbchar6_compat: 'E3/DC efy/Easy – WBchar6-Kompatibilitätsregelung aktiv',
                status_only: 'Nur Status – WBchar6 nicht gewählt; direkte Schreibausgänge gesperrt'
            };
            const family = String(wb.e3dc_device_family || 'unknown').toLowerCase();
            const backend = String(wb.e3dc_control_backend || 'status_only').toLowerCase();
            const familyLabel = familyLabels[family] || familyLabels.unknown;
            const runtimeBackendLabel = String(wb.e3dc_backend_label || '');
            const backendLabel = backend === 'wbchar6_compat' || backend === 'status_only'
                ? backendLabels[backend]
                : String(runtimeBackendLabel || backendLabels[backend] || backendLabels.status_only);
            const parts = [
                familyLabel,
                wb.firmware_version ? ('Firmware ' + String(wb.firmware_version)) : '',
                wb.e3dc_rscp_wallbox_type !== null && wb.e3dc_rscp_wallbox_type !== undefined
                    ? ('beobachteter RSCP-Typ ' + String(wb.e3dc_rscp_wallbox_type)) : '',
                wb.e3dc_direct_readback_complete === true
                    ? 'Sun/Auto/Abort-Readback vorhanden und typgültig; nur Diagnose'
                    : 'direkter Readback unvollständig oder nicht frisch',
                'direkte Schreibausgänge nicht freigegeben (no-send)',
                backendLabel
            ].filter(Boolean);
            return {
                label: backendLabel,
                familyLabel,
                detail: parts.join('\n'),
                className: backend === 'wbchar6_compat' ? 'text-info' : 'text-muted'
            };
        }

        function nativeWallboxLooksActive(data) {
            if (!data) return false;
            const status = String(data.status_msg || '').toLowerCase();
            const statusNorm = status.replace(/ä/g, 'ae');
            const terminalCode = ['vehicle_charge_done', 'battery_departure_done']
                .includes(String(data.operator_hint_code || '').toLowerCase());
            const terminalStatus = ['ladung beendet', 'beendet', 'kein fahrzeug', 'wartet mindestleistung', 'warte auf sonne', 'idle']
                .some(token => statusNorm.includes(token));
            if (terminalCode || terminalStatus) return false;
            const power = Math.abs(parseFloat(data.total_power_w || 0));
            const setAmp = parseFloat(data.set_amp || 0);
            const capAmp = parseFloat(data.cap_amp || 0);
            const activeStatus = ['laedt', 'lädt', 'lade ', 'lade mit', 'lade parallel', 'startfreigabe', 'freigegeben']
                .some(token => statusNorm.includes(token));
            return data.charging_active === true || power > 500 || (activeStatus && (setAmp > 0 || capAmp > 0));
        }

        function smoothNativeWallboxData(rawData) {
            const nowMs = Date.now();
            const previous = nativeWallboxDisplayCache && (nowMs - nativeWallboxDisplayCache.seenMs < nativeWallboxHoldMs)
                ? nativeWallboxDisplayCache.data
                : null;
            const data = Object.assign({}, rawData || {});
            const prevActive = nativeWallboxLooksActive(previous);
            const curActive = nativeWallboxLooksActive(data);
            const prevPower = Math.abs(parseFloat(previous?.total_power_w || 0));
            let curPower = Math.abs(parseFloat(data.total_power_w || 0));
            let held = false;

            if (previous && prevActive && curActive && prevPower > 500) {
                if (curPower <= 50) {
                    data.total_power_w = previous.total_power_w;
                    curPower = prevPower;
                    held = true;
                }
                ['set_amp', 'cap_amp', 'detected_phases', 'fuzzy_factor', 'status_msg', 'wb_type', 'wb_control_label', 'wb_control_status', 'wb_control_detail', 'wb_control_level'].forEach(key => {
                    const v = data[key];
                    const isZeroAmpStatus = key === 'status_msg' && String(v || '').replace(/\s/g, '').toLowerCase().includes('0a');
                    if ((isZeroAmpStatus || v === undefined || v === null || v === '' || v === 0 || v === '0') &&
                        previous[key] !== undefined && previous[key] !== null && previous[key] !== '' && previous[key] !== 0 && previous[key] !== '0') {
                        data[key] = previous[key];
                        held = true;
                    }
                });

                const phases = Math.max(1, Math.min(3, parseInt(data.detected_phases || previous.detected_phases || 3, 10)));
                const ampLimit = Math.max(parseFloat(data.set_amp || 0), parseFloat(data.cap_amp || 0));
                const expectedW = ampLimit * 230 * phases;
                if (curPower > 1000 && ((expectedW > 0 && curPower > expectedW * 1.45) || (expectedW <= 0 && curPower > 18000))) {
                    data.total_power_w = previous.total_power_w;
                    held = true;
                }
            }

            if (nativeWallboxLooksActive(data) || Math.abs(parseFloat(data.total_power_w || 0)) > 50) {
                nativeWallboxDisplayCache = { data: Object.assign({}, data), seenMs: nowMs };
            }
            data.ui_hold = data.ui_hold || held;
            return data;
        }

        const nativeWallboxEnabled = <?= $nativeWallboxStatusEnabled ? 'true' : 'false' ?>;
        function setNativeWallboxColumnsVisible(visible) {
            const shouldShow = nativeWallboxEnabled && visible === true;
            const col2 = document.getElementById('wb-native-col2');
            const col3 = document.getElementById('wb-native-col3');
            [col2, col3].forEach(col => {
                if (!col) return;
                col.hidden = !shouldShow;
                col.setAttribute('aria-hidden', shouldShow ? 'false' : 'true');
                col.classList.toggle('d-none', !shouldShow);
                col.classList.toggle('d-flex', shouldShow);
                if (shouldShow) {
                    col.style.removeProperty('display');
                } else {
                    col.style.setProperty('display', 'none', 'important');
                }
            });
        }

        async function updateNativeWallboxBanner() {
            try {
                if (!nativeWallboxEnabled) {
                    setNativeWallboxColumnsVisible(false);
                    return;
                }
                // Wir hängen einen Timestamp an, um Caching zu umgehen
                const response = await fetch('get_live_json.php?wallbox_native_snapshot=1&t=' + new Date().getTime());
                let hideWallboxData = false;
                let data = {};

                if (!response.ok) {
                    hideWallboxData = true;
                } else {
                    data = await response.json();
                    const now = Math.floor(Date.now() / 1000);
                    if (!data.ts || Math.abs(now - data.ts) > 180) {
                        hideWallboxData = true;
                    }
                }
                if (hideWallboxData && nativeWallboxDisplayCache && (Date.now() - nativeWallboxDisplayCache.seenMs < 600000)) {
                    data = Object.assign({ ui_hold: true }, nativeWallboxDisplayCache.data);
                    hideWallboxData = false;
                } else if (!hideWallboxData) {
                    data = smoothNativeWallboxData(data);
                }

                // Kachel-Sichtbarkeit: Wenn Wallbox konfiguriert ist, Spalten immer halten
                if (hideWallboxData && !nativeWallboxEnabled) {
                    setNativeWallboxColumnsVisible(false);
                    return;
                }
                setNativeWallboxColumnsVisible(true);

                // Kachel einblenden
                const wbAlert = document.getElementById('wb-native-alert');
                if(wbAlert.style.display === 'none') {
                    // Start-Animation
                    wbAlert.style.display = 'block';
                    wbAlert.style.opacity = 0;
                    setTimeout(() => { wbAlert.style.transition = 'opacity 0.5s'; wbAlert.style.opacity = 1; }, 50);
                }

                // Werte einfügen
                let wbDetails = Array.isArray(data.wb_details) ? data.wb_details : [];
                if (data.wb_multi_contract && data.wb_multi_contract.slots && typeof data.wb_multi_contract.slots === 'object') {
                    const slotMap = data.wb_multi_contract.slots;
                    const existingIds = new Set(wbDetails.map(w => parseInt(w.id, 10)));
                    Object.keys(slotMap).forEach(key => {
                        const slot = slotMap[key];
                        if (slot && slot.id && !existingIds.has(parseInt(slot.id, 10))) {
                            wbDetails.push({
                                id: parseInt(slot.id, 10),
                                amp: slot.effective_amp || slot.allocated_amp || 0,
                                current_set_amp: slot.allocated_amp || 0,
                                cap_amp: 0,
                                target_amp: slot.effective_amp || 0,
                                status_amp: slot.effective_amp || 0,
                                state: slot.reason === 'no_vehicle' ? 'Idle' : (slot.running ? 'Lade' : (slot.reason || 'Idle')),
                                state_level: slot.running ? 'success' : 'secondary',
                                state_reason: slot.reason || '',
                                power_w: 0,
                                plug: slot.connected || false,
                                charging: slot.running || false,
                                max_amp: 32
                            });
                        }
                    });
                }
                const dashboardWb2Configured = <?= !empty($dashHasWb2) ? 'true' : 'false' ?>;
                const wbCount = (dashboardWb2Configured || (data.wb_type && String(data.wb_type).toLowerCase().includes('multi'))) ? Math.max(2, wbDetails.length) : 1;
                if ((dashboardWb2Configured || wbCount > 1) && wbDetails.length < 2) {
                    const existingIds = new Set(wbDetails.map(w => parseInt(w.id, 10)));
                    [1, 2].forEach(id => {
                        if (!existingIds.has(id)) {
                            wbDetails.push({
                                id: id,
                                amp: 0,
                                current_set_amp: 0,
                                cap_amp: 0,
                                target_amp: 0,
                                status_amp: 0,
                                state: 'Idle',
                                state_level: 'secondary',
                                state_reason: 'Standby',
                                power_w: 0,
                                plug: false,
                                charging: false,
                                max_amp: 32
                            });
                        }
                    });
                }
                const wbPriorityModeRaw = data.wb_priority_mode ?? data.wb_native_distribution_mode ?? data.wb_distribution_mode ?? 0;
                const wbPriorityMode = [1, 2].includes(parseInt(wbPriorityModeRaw, 10)) ? parseInt(wbPriorityModeRaw, 10) : 0;
                const wbPriorityLabel = wbPriorityMode === 1 ? 'Prio WB1' : (wbPriorityMode === 2 ? 'Prio WB2' : (wbCount > 1 ? 'Balance' : ''));
                const fmtAmp = (amp, minPrecision = 0) => {
                    const value = parseFloat(amp || 0);
                    if (!Number.isFinite(value) || value <= 0) return '0';
                    return minPrecision > 0 || !Number.isInteger(value)
                        ? value.toLocaleString('de-DE', {minimumFractionDigits: 1, maximumFractionDigits: 1})
                        : String(Math.round(value));
                };
                const fmtKw = (watts) => (Math.max(0, Number(watts) || 0) / 1000)
                    .toLocaleString('de-DE', {minimumFractionDigits: 1, maximumFractionDigits: 1});
                const boolValue = (value) => value === true || value === 1 || value === '1' || String(value).toLowerCase() === 'true';
                const wallboxPhasePowerCount = (wb) => ['phase_power_l1_w', 'phase_power_l2_w', 'phase_power_l3_w']
                    .reduce((count, key) => count + (Math.abs(parseFloat(wb[key] || 0)) > 250 ? 1 : 0), 0);
                const wallboxConfirmedCharging = (wb, power, state) => {
                    const chargeTruth = String(wb.charge_truth || wb.charge_contract?.truth || '').toLowerCase();
                    const chargeSource = String(wb.charge_source || wb.charge_contract?.source || '').toLowerCase();
                    const sessionState = String(wb.openwb_pro_session_state || '').toLowerCase();
                    const blockedState = state.includes('stop') || state.includes('wartet mindestleistung') || state.includes('start wartet');
                    const transitionOnly = sessionState === 'phase_wait' || chargeSource.includes('phantom') || chargeTruth === 'not_charging' || chargeTruth === 'stop_pending';
                    if (blockedState || transitionOnly) return false;
                    if (chargeTruth === 'charging') return power > 50;
                    return power > 500 && (wb.charging === true || state === 'lade' || state.includes('lädt') || state.includes('laedt'));
                };
                const fractionalAmpInfo = (wb, fallbackAmp) => {
                    const rawAmp = parseFloat(wb.offered_current_raw ?? 0) || 0;
                    const stepAmp = parseFloat(wb.current_step_amp ?? 1) || 1;
                    const fineStep = boolValue(wb.fractional_current_supported)
                        || stepAmp <= 0.11
                        || (rawAmp > 0 && Math.abs(rawAmp - Math.round(rawAmp)) > 0.001);
                    const displayAmp = fineStep && rawAmp > 0 ? rawAmp : fallbackAmp;
                    return {
                        displayAmp,
                        precision: fineStep && displayAmp > 0 ? 1 : 0,
                        fineStep,
                        rawAmp
                    };
                };
                const fineAmpLabel = (rows) => rows
                    .filter(wb => wb.fineStep && wb.rawAmp > 0)
                    .map(wb => 'WB' + wb.id + ' ' + fmtAmp(wb.rawAmp, 1) + ' A')
                    .join(', ');
                const bestAmpPrecision = (rows) => rows.some(wb => wb.precision > 0 && wb.displaySetAmp > 0) ? 1 : 0;
                const wbAmpRows = wbDetails.map(wb => {
                    const id = parseInt(wb.id, 10) || 0;
                    const rawSetAmp = parseFloat(wb.current_set_amp);
                    const setAmp = (!isNaN(rawSetAmp) && rawSetAmp > 0) ? rawSetAmp : (parseFloat(wb.target_amp || wb.amp || 0) || 0);
                    const rawCapAmp = parseFloat(wb.cap_amp);
                    const capAmp = (!isNaN(rawCapAmp) && rawCapAmp > 0) ? rawCapAmp : (parseFloat(wb.target_amp || wb.amp || 0) || 0);
                    const statusAmp = parseFloat(wb.status_amp || wb.amp || 0) || 0;
                    const amp = parseFloat(wb.amp) || setAmp;
                    const setAmpInfo = fractionalAmpInfo(wb, setAmp);
                    const ampInfo = fractionalAmpInfo(wb, amp);
                    const power = Math.abs(parseFloat(wb.power_w || wb.phase_power_sum_w || 0));
                    const state = String(wb.state || '').toLowerCase();
                    const realCharging = wallboxConfirmedCharging(wb, power, state);
                    const startRelease = state.includes('startfreigabe');
                    const rawPhases = parseInt(wb.phases_in_use || wb.phases_actual || wb.phase_actual_phases || wb.phases_target || 0, 10) || 0;
                    const measuredPhases = wallboxPhasePowerCount(wb);
                    const phases = realCharging ? (measuredPhases || rawPhases) : 0;
                    const rawApparentKva = parseFloat(wb.apparent_power_kva);
                    const rawApparentVa = parseFloat(wb.apparent_power_va);
                    const apparentKva = Number.isFinite(rawApparentKva) && rawApparentKva > 0
                        ? rawApparentKva
                        : (Number.isFinite(rawApparentVa) && rawApparentVa > 0 ? rawApparentVa / 1000 : null);
                    return {
                        id,
                        amp,
                        setAmp,
                        displayAmp: ampInfo.displayAmp,
                        displaySetAmp: setAmpInfo.displayAmp,
                        precision: Math.max(ampInfo.precision, setAmpInfo.precision),
                        fineStep: ampInfo.fineStep || setAmpInfo.fineStep,
                        rawAmp: setAmpInfo.rawAmp || ampInfo.rawAmp,
                        capAmp,
                        statusAmp,
                        power,
                        realCharging,
                        startRelease,
                        phases,
                        apparentKva
                    };
                });
                const fallbackFineAmp = fractionalAmpInfo({
                    offered_current_raw: data.wb_offered_current_raw,
                    current_step_amp: data.wb_current_step_amp,
                    fractional_current_supported: data.wb_fractional_current_supported
                }, parseFloat(data.set_amp || data.wb_set_amp || 0) || 0);
                const wbOfferedAmp = wbAmpRows.length
                    ? wbAmpRows.reduce((sum, wb) => sum + wb.displaySetAmp, 0)
                    : fallbackFineAmp.displayAmp;
                const wbActiveAmp = wbAmpRows.length
                    ? wbAmpRows.filter(wb => wb.realCharging).reduce((sum, wb) => sum + wb.displaySetAmp, 0)
                    : wbOfferedAmp;
                const wbDisplayAmp = wbActiveAmp > 0 ? wbActiveAmp : wbOfferedAmp;
                const wbDisplayPrecision = wbAmpRows.length
                    ? bestAmpPrecision(wbActiveAmp > 0 ? wbAmpRows.filter(wb => wb.realCharging) : wbAmpRows)
                    : fallbackFineAmp.precision;
                const wbFineAmpLabel = fineAmpLabel(wbAmpRows);
                const wbAmpRowById = new Map(wbAmpRows.map(wb => [wb.id, wb]));
                const wbStartReleaseAmp = wbAmpRows.filter(wb => wb.startRelease && !wb.realCharging).reduce((sum, wb) => sum + wb.displayAmp, 0);
                const wbCountEl = document.getElementById('wb-native-count');
                if (wbCountEl) wbCountEl.textContent = wbCount + ' WB';
                const typedTargetPowerW = Math.max(0, parseFloat(data.set_power_w || 0));
                const finiteBudgetW = (value) => {
                    const parsed = Number(value);
                    return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
                };
                const powerLedgerCandidate = data.wb_multi_contract
                    && typeof data.wb_multi_contract === 'object'
                    && data.wb_multi_contract.power_ledger
                    && typeof data.wb_multi_contract.power_ledger === 'object'
                    ? data.wb_multi_contract.power_ledger
                    : null;
                const wbPowerLedger = powerLedgerCandidate
                    && powerLedgerCandidate.schema_version === 'wallbox_group_power_ledger_v1'
                    ? powerLedgerCandidate
                    : null;
                const wbGroupGrossBudgetW = wbPowerLedger
                    ? finiteBudgetW(wbPowerLedger.gross_group_budget_w)
                    : null;
                const wbGroupUnmanagedReservedW = wbPowerLedger
                    ? finiteBudgetW(wbPowerLedger.unmanaged_reserved_w)
                    : null;
                const wbGroupManagedBudgetW = wbPowerLedger
                    ? finiteBudgetW(wbPowerLedger.managed_budget_w)
                    : null;
                const fallbackGroupCapW = finiteBudgetW(data.wb_effective_budget_w);
                const wbGroupCapW = wbGroupGrossBudgetW !== null
                    ? wbGroupGrossBudgetW
                    : fallbackGroupCapW;
                const getWbPhases = (wb) => {
                    if (wb && wb.phases > 0) return wb.phases;
                    const p = parseInt(
                        (wb && wb.phases_target) ||
                        (wb && (wb.phases_in_use || wb.phases_actual || wb.phase_actual_phases)) ||
                        (data && (data.phases_target || data.phases_in_use || data.detected_phases)) ||
                        3, 10
                    );
                    return (p === 1 || p === 3) ? p : 3;
                };
                const calcTargetPowerW = wbAmpRows.reduce((sum, wb) => {
                    const ph = getWbPhases(wb);
                    return sum + (wb.displaySetAmp * 230 * ph);
                }, 0);
                const targetPowerW = (wbAmpRows.length > 0 && calcTargetPowerW > 0) ? calcTargetPowerW : (typedTargetPowerW > 0 ? typedTargetPowerW : calcTargetPowerW);
                const typedPhaseAmp = Array.isArray(data.set_phase_amp) ? data.set_phase_amp.map(v => parseFloat(v || 0)) : null;
                const multiPowerDisplay = wbCount > 1;
                const wbPrimaryPowerW = wbGroupCapW !== null ? wbGroupCapW : typedTargetPowerW;
                const wbAmpLabel = multiPowerDisplay
                    ? (wbGroupCapW !== null ? 'Leistungsbudget' : 'Nominalfreigabe')
                    : 'Regel-Soll';
                const wbAmpTitleParts = [];
                if (wbCount > 1 || wbAmpRows.length > 0) {
                    wbAmpRows.forEach(wb => {
                        const ph = getWbPhases(wb);
                        const phaseLabel = ph > 0 ? ph + 'p' : '3p';
                        const kwVal = (wb.displaySetAmp * 230 * ph) / 1000;
                        wbAmpTitleParts.push('WB' + wb.id + ': ' + fmtAmp(wb.displaySetAmp, wb.precision) + ' A × ' + phaseLabel + ' · ' + kwVal.toFixed(1).replace('.', ',') + ' kW');
                    });
                    wbAmpTitleParts.push('Nominale Stromfreigabe gesamt: ' + (targetPowerW / 1000).toFixed(1).replace('.', ',') + ' kW');
                    if (wbGroupCapW !== null) {
                        wbAmpTitleParts.push('Wirksames Gruppenbudget: ' + fmtKw(wbGroupCapW) + ' kW');
                    }
                    if (wbGroupManagedBudgetW !== null && wbGroupUnmanagedReservedW !== null) {
                        wbAmpTitleParts.push('Davon steuerbar: ' + fmtKw(wbGroupManagedBudgetW) + ' kW · bereits gebunden: ' + fmtKw(wbGroupUnmanagedReservedW) + ' kW');
                    }
                    if (multiPowerDisplay) {
                        wbAmpTitleParts.push('Stromstärke und Phasenzahl bleiben je Wallbox in den Details sichtbar.');
                    }
                    if (typedPhaseAmp && typedPhaseAmp.length === 3) {
                        wbAmpTitleParts.push('Phasenvektor am Netzpunkt: L1 ' + fmtAmp(typedPhaseAmp[0], 1) + ' / L2 ' + fmtAmp(typedPhaseAmp[1], 1) + ' / L3 ' + fmtAmp(typedPhaseAmp[2], 1) + ' A');
                    }
                    if (wbStartReleaseAmp > 0) {
                        wbAmpTitleParts.push('Davon Startfreigabe ohne echte Ladeleistung: ' + fmtAmp(wbStartReleaseAmp, wbDisplayPrecision) + ' A');
                    }
                    if (wbFineAmpLabel) wbAmpTitleParts.push('0,1-A-Feinregelung aktiv: ' + wbFineAmpLabel);
                } else if (wbFineAmpLabel) {
                    wbAmpTitleParts.push('0,1-A-Feinregelung aktiv: ' + wbFineAmpLabel);
                }
                const wbAmpLabelEl = document.getElementById('wb-native-amp-label');
                const wbAmpEl = document.getElementById('wb-native-amp');
                const wbAmpUnitEl = document.getElementById('wb-native-amp-unit');
                if (wbAmpLabelEl) {
                    wbAmpLabelEl.textContent = wbAmpLabel;
                    wbAmpLabelEl.title = wbAmpTitleParts.join('\n');
                }
                const e3dcInfo = nativeWallboxE3dcInfo(wbDetails);
                document.getElementById('wb-native-type').textContent = wbCount > 1
                    ? `Multi (${wbCount} WB)`
                    : (e3dcInfo.familyLabel || data.wb_type || 'Wallbox');
                if (wbAmpEl) {
                    wbAmpEl.textContent = multiPowerDisplay
                        ? (wbPrimaryPowerW / 1000).toFixed(1).replace('.', ',')
                        : fmtAmp(wbDisplayAmp, wbDisplayPrecision);
                    wbAmpEl.title = wbAmpTitleParts.join('\n');
                }
                if (wbAmpUnitEl) wbAmpUnitEl.textContent = multiPowerDisplay ? 'kW' : 'A';
                const wbNativeStatusEl = document.getElementById('wb-native-status');
                if (wbNativeStatusEl) {
                    const nativeOperatorHint = data.operator_hint || data.status_msg || 'Bereit';
                    const nativeHintCode = String(data.operator_hint_code || '');
                    const nativeHintLevel = String(data.operator_hint_level || 'info');
                    const nativeHintClasses = {
                        success: 'text-success',
                        warning: 'text-warning',
                        danger: 'text-danger',
                        secondary: 'text-muted',
                        info: 'text-body'
                    };
                    const wbFloorNote = String(data.wbminsoc_floor_note || '');
                    let nativeStatusText = nativeOperatorHint;
                    let nativeStatusDetail = '';
                    if (nativeHintCode === 'no_vehicle') {
                        nativeStatusText = 'Kein Fahrzeug';
                        nativeStatusDetail = 'Einstellungen gespeichert';
                    }
                    wbNativeStatusEl.textContent = nativeStatusText;
                    wbNativeStatusEl.title = nativeOperatorHint;
                    wbNativeStatusEl.className = nativeHintClasses[nativeHintLevel] || nativeHintClasses.info;
                    const statusDetailEl = document.getElementById('wb-native-status-detail');
                    if (statusDetailEl) {
                        const wallboxType = wbCount > 1 ? `Multi (${wbCount} WB)` : (e3dcInfo.familyLabel || data.wb_type || 'Wallbox');
                        const controlInfo = nativeWallboxControlInfo(data, wbDetails);
                        const rscpInfo = nativeWallboxRscpInfo(data, wbDetails);
                        const controlHtml = controlInfo.label
                            ? ' · <span class="' + controlInfo.className + '">' + escapeHtmlText(controlInfo.label) + '</span>'
                            : '';
                        const floorHtml = wbFloorNote
                            ? ' &middot; <span class="text-warning">E3DC-Untergrenze aktiv</span>'
                            : '';
                        const rscpHtml = rscpInfo.label
                            ? ' | <span class="' + rscpInfo.className + '">' + escapeHtmlText(rscpInfo.label) + '</span>'
                            : '';
                        const e3dcHtml = e3dcInfo.label
                            ? ' | <span class="' + e3dcInfo.className + '">' + escapeHtmlText(e3dcInfo.label) + '</span>'
                            : '';
                        const priorityHtml = wbPriorityLabel
                            ? ' · <span class="' + (wbPriorityMode ? 'text-info fw-bold' : 'text-muted') + '">' + escapeHtmlText(wbPriorityLabel) + '</span>'
                            : '';
                        statusDetailEl.innerHTML = '<i class="fas fa-charging-station text-warning me-1" style="font-size:0.65rem;"></i><span id="wb-native-type">' + escapeHtmlText(wallboxType) + '</span>' + priorityHtml + (nativeStatusDetail ? ' · ' + escapeHtmlText(nativeStatusDetail) : '') + controlHtml;
                        statusDetailEl.innerHTML += floorHtml;
                        statusDetailEl.title = [nativeOperatorHint, wbFloorNote, e3dcInfo.detail, controlInfo.detail, rscpInfo.detail].filter(Boolean).join('\n');
                    }
                }

                // --- WB Regelung Panel ---
                // Modus-Badge
                const wbModeBadge = document.getElementById('wb-mode-badge');
                if (wbModeBadge) {
                    const modeNum = data.wb_mode_active || data.wb_mode || 0;
                    const isExternalWb = !!data.is_external_wb;
                    const modeLabels = {
                        0: isExternalWb ? 'Nur beobachten' : 'E3DC regelt',
                        1: 'PV-Kurve',
                        2: 'PV-Kurve',
                        3: 'Grundladung',
                        4: 'PV + Akku',
                        5: 'Preislimit',
                        6: 'Grundladung',
                        7: 'PV-Kurve',
                        8: 'PV-Kurve',
                        9: 'PV + Akku',
                        10: 'PV + Akku',
                        11: 'Preislimit'
                    };
                    const modeTitles = {
                        0: isExternalWb ? 'E3DC-Control sendet keine Ladebefehle; Wallbox oder Fremdregelung regelt selbst.' : 'E3DC-Control sendet keine Start- oder Strombefehle; E3DC regelt selbst.',
                        1: 'Legacy-Alias für PV-Kurve ruhig.',
                        2: 'Lädt entlang der Speicher-Ladekurve mit Hysterese und ruhiger Regelung.',
                        3: 'Hält eine stabile Grundladung gegen Schütz-Flattern, solange das Speicherziel erreichbar bleibt.',
                        4: 'Auto darf PV und Hausakku bis zur Hausakku-Reserve nutzen. Netz bleibt aus; darunter stützt der Akku nur Hausverbrauch und Wärmepumpe.',
                        5: 'Sofortladen mit Netzstrom nur bis zum eingestellten Preislimit.',
                        6: 'Legacy-Alias für Grundladung stabil.',
                        7: 'Legacy-Alias für PV-Kurve ruhig.',
                        8: 'Legacy-Alias für PV-Kurve ruhig.',
                        9: 'Legacy-Alias für PV + Akku bis Untergrenze.',
                        10: 'Legacy-Alias für PV + Akku bis Untergrenze.',
                        11: 'Legacy-Alias für Sofort bis Preislimit.'
                    };
                    const isBatteryReserveMode = (modeNum === 4 || modeNum === 9 || modeNum === 10);
                    const reserveSocCandidates = [
                        data.wbminsoc_effective_soc,
                        data.wbminsoc_configured_soc,
                        data.wbminsoc,
                        data.dynamic_min_soc,
                        data.bat_floor_soc
                    ];
                    const reserveSocRaw = reserveSocCandidates.find(value => value !== null && value !== undefined && value !== '');
                    const reserveSoc = reserveSocRaw !== undefined ? Number.parseFloat(reserveSocRaw) : NaN;
                    const reserveSocLabel = Number.isFinite(reserveSoc)
                        ? reserveSoc.toLocaleString('de-DE', { maximumFractionDigits: reserveSoc % 1 === 0 ? 0 : 1 }) + '%'
                        : '';
                    let modeLabel = (modeLabels[modeNum] || 'Modus '+modeNum);
                    let modeTitle = modeTitles[modeNum] || '';
                    if (isBatteryReserveMode && reserveSocLabel) {
                        modeLabel = 'PV + Akku bis ' + reserveSocLabel;
                        modeTitle += (modeTitle ? '\n' : '') + 'Hausakku-Reserve: ' + reserveSocLabel;
                    } else if (isBatteryReserveMode &&
                        (data.battery_request === 'hold_discharge' || data.wbminsoc_gate_open === false)) {
                        modeLabel = 'PV + Akku bis Reserve';
                    }
                    wbModeBadge.textContent = modeLabel;
                    wbModeBadge.title = modeTitle;
                    // Farbe nach Modus
                    const modeColors = {0:'secondary',1:'success',2:'info',3:'info',4:'primary',5:'warning',6:'warning',7:'info',8:'info',9:'success',10:'success',11:'danger'};
                    wbModeBadge.className = 'badge rounded-pill bg-' + (modeColors[modeNum]||'secondary') + ' bg-opacity-25 text-' + (modeColors[modeNum]||'secondary');
                }
                // Batterie-Sperre
                const batEl = document.getElementById('wb-native-batstate');
                if (batEl) {
                    if(data.rscp_bat_lock) {
                        if (data.status_msg && data.status_msg.toLowerCase().includes('netz')) {
                            batEl.innerHTML = '<span class="text-danger" style="font-size:0.7rem;"><i class="fas fa-bolt me-1"></i>Netzladen</span>';
                        } else {
                            batEl.innerHTML = '<span class="text-warning" style="font-size:0.7rem;"><i class="fas fa-lock me-1"></i>Bat-Sperre</span>';
                        }
                    } else {
                        batEl.innerHTML = '<span class="text-success" style="font-size:0.7rem;"><i class="fas fa-unlock me-1"></i>Normal</span>';
                    }
                }
                // Fuzzy + Cap + Phasen
                const fzEl = document.getElementById('wb-fuzzy-factor');
                if (fzEl) fzEl.textContent = data.fuzzy_factor != null ? parseFloat(data.fuzzy_factor).toFixed(2) : '--';
                const capEl = document.getElementById('wb-cap-amp');
                if (capEl) {
                    capEl.textContent = wbGroupCapW !== null ? fmtKw(wbGroupCapW) + ' kW' : '--';
                    capEl.title = wbGroupCapW !== null
                        ? 'Typisiertes wirksames Gruppenbudget der Wallbox-Regelung.'
                        : 'Kein belastbarer Gruppen-Leistungsdeckel verfügbar.';
                }
                const phEl = document.getElementById('wb-phases-badge');
                if (phEl) {
                    const detailPhases = wbAmpRows.reduce((max, wb) => wb.realCharging ? Math.max(max, wb.phases || 0) : max, 0);
                    const activePhases = parseInt(data.active_wb_phases || 0, 10) || 0;
                    const ph = detailPhases > 0 ? detailPhases : (data.charging_active === true ? activePhases : 0);
                    phEl.textContent = ph > 0 ? ph + 'ph' : '--ph';
                    phEl.style.color = ph === 3 ? '#10b981' : ph === 2 ? '#f59e0b' : (ph === 1 ? '#818cf8' : '#94a3b8');
                    phEl.title = ph > 0
                        ? 'Bestätigte aktive Phasen der Wallbox-Regelung.'
                        : 'Phasenwechsel- und Stop-Nachlaufwerte werden ausgeblendet.';
                    phEl.style.display = multiPowerDisplay ? 'none' : 'inline';
                }
                const kvaEl = document.getElementById('wb-native-kva-badge');
                if (kvaEl) {
                    const detailKva = wbDetails.reduce((sum, wb) => {
                        const kva = parseFloat(wb.apparent_power_kva || 0);
                        const va = parseFloat(wb.apparent_power_va || 0);
                        return sum + (kva > 0 ? kva : (va > 0 ? va / 1000 : 0));
                    }, 0);
                    const detailPower = wbDetails.reduce((sum, wb) => {
                        const power = Math.abs(parseFloat(wb.power_w || wb.phase_power_sum_w || 0));
                        return sum + (Number.isFinite(power) ? power : 0);
                    }, 0);
                    const nativeKva = detailKva > 0 ? detailKva : parseFloat(data.apparent_power_kva || 0);
                    if (multiPowerDisplay) {
                        kvaEl.title = 'Scheinleistung und Phasen sind ladepunktspezifisch und stehen in den Wallbox-Details.';
                        kvaEl.style.display = 'none';
                    } else if (nativeKva > 0.05) {
                        const pf = detailPower > 50 ? Math.max(0, Math.min(1, detailPower / (nativeKva * 1000))) : 0;
                        const pfText = pf > 0 ? ' · LF ' + pf.toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2}) : '';
                        kvaEl.textContent = nativeKva.toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' kVA' + pfText;
                        kvaEl.title = 'Scheinleistung aus Spannung x Strom je Phase. LF ist der Leistungsfaktor; die große Wallbox-Zahl bleibt die Wirkleistung in kW.';
                        kvaEl.style.display = 'inline';
                    } else {
                        kvaEl.style.display = 'none';
                    }
                }

                // Pulsation basierend auf Lade-Satus
                const pulseEl = document.getElementById('wb-native-pulse');
                if(data.charging_active) {
                    pulseEl.className = "rounded-circle me-3 active-pulse";
                    pulseEl.style.background = "#2ecc71"; // Grün wenn aktiv lädt
                } else {
                    pulseEl.className = "rounded-circle me-3";
                    pulseEl.style.background = data.connected ? "#f39c12" : "#6c757d"; // Orange (warten) oder Grau (nicht verbunden)
                }

                // Multi-Wallbox Details anzeigen, falls vorhanden
                const multiDiv = document.getElementById('wb-native-multi-details');
                const showMultiSlots = multiDiv && (wbDetails.length > 1 || wbCount > 1);
                if (multiDiv) {
                    multiDiv.classList.toggle('d-none', !showMultiSlots);
                }
                if(showMultiSlots) {
                    multiDiv.classList.remove('d-none');
                    wbDetails.forEach(wb => {
                        const wbId = parseInt(wb.id, 10);
                        const curAmpEl = document.getElementById('wb-native-'+wb.id+'-amp');
                        const curStateEl = document.getElementById('wb-native-'+wb.id+'-state');
                        const curDotEl = document.getElementById('wb-native-'+wb.id+'-dot');
                        const curSlotEl = document.getElementById('wb-native-'+wb.id+'-slot');
                        const curPrioEl = document.getElementById('wb-native-'+wb.id+'-priority');
                        const stateBaseText = String(wb.state || 'Idle');
                        let stateText = stateBaseText;
                        const transition = wb.transition_state && typeof wb.transition_state === 'object' ? wb.transition_state : {};
                        const transitionStage = String(transition.state || 'idle').toLowerCase();
                        const transitionTarget = parseInt(transition.target_phases || 0, 10);
                        const transitionRemainingS = Math.max(0, Math.ceil(parseFloat(transition.remaining_s || 0)));
                        const transitionLabels = {
                            ramp_to_zero: 'Strom wird auf 0 A reduziert',
                            zero_settle: '0-A-Beruhigungszeit',
                            set_phase: 'Phasenziel wird gesetzt',
                            cp_interrupt: 'CP-Unterbrechung',
                            restart_delay: 'Wiederanlauf-Wartezeit',
                            confirm_target: 'Zielphasen werden bestätigt',
                            recovery_hold: 'Ausgangsbestätigung wird sicher geprüft',
                            fault: 'Störung'
                        };
                        const transitionActive = transition.active === true
                            && Object.prototype.hasOwnProperty.call(transitionLabels, transitionStage);
                        const stopS = parseInt(wb.openwb_pro_session_stop_remaining_s || 0, 10);
                        const startHoldS = parseInt(wb.openwb_pro_session_start_hold_remaining_s || 0, 10);
                        const wakeupS = parseInt(wb.openwb_pro_session_wakeup_remaining_s || 0, 10);
                        if (transitionActive) {
                            const direction = transitionTarget > 0 ? ` → ${transitionTarget}p` : '';
                            const countdown = transitionRemainingS > 0 ? ` · ${transitionRemainingS}s` : '';
                            stateText = `Phasenwechsel${direction}: ${transitionLabels[transitionStage]}${countdown}`;
                        } else if (wakeupS > 0) {
                            stateText += ` (Wake-up ${wakeupS}s)`;
                        } else if (startHoldS > 0) {
                            stateText += ` (Start-Sperre ${startHoldS}s)`;
                        } else if (stopS > 0 && (stateBaseText.toLowerCase().includes('stopp') || stateBaseText.toLowerCase().includes('stop'))) {
                            stateText += ` (Stopp-Sperre ${stopS}s)`;
                        }
                        const phaseCooldown = wb.phase_cooldown && typeof wb.phase_cooldown === 'object' ? wb.phase_cooldown : {};
                        const cooldownS = Math.max(0, Math.ceil(parseFloat(phaseCooldown.remaining_s || 0)));
                        if (!transitionActive && phaseCooldown.active === true && cooldownS > 0) {
                            stateText += ` · Phasensperre ${cooldownS}s`;
                        }
                        const stateLevel = String(wb.state_level || '').toLowerCase();
                        const controlLabel = String(wb.control_label || '');
                        const controlDetail = String(wb.control_detail || controlLabel);
                        const rscpProblem = wb.rscp_error_active === true || String(wb.rscp_status || '').toLowerCase() === 'error';
                        const rscpReason = rscpProblem ? ('RSCP: ' + String(wb.rscp_last_error || 'Zugriff fehlgeschlagen')) : '';
                        const stateReason = [String(wb.state_reason || stateBaseText), controlLabel ? (controlLabel + (controlDetail && controlDetail !== controlLabel ? ': ' + controlDetail : '')) : ''].filter(Boolean).join(' · ');
                        const stateClassMap = {
                            success: 'fw-bold text-success',
                            warning: 'fw-bold text-warning',
                            danger: 'fw-bold text-danger',
                            secondary: 'fw-normal text-muted',
                            info: 'fw-normal text-info'
                        };
                        const stateDotMap = {
                            success: '#22c55e',
                            warning: '#f59e0b',
                            danger: '#ef4444',
                            secondary: '#6c757d',
                            info: '#22d3ee'
                        };
                        if (curAmpEl) {
                            const ampRow = wbAmpRowById.get(wbId);
                            const displayAmp = ampRow ? ampRow.displayAmp : (parseFloat(wb.amp) || 0);
                            const precision = ampRow ? ampRow.precision : 0;
                            const slotPhases = ampRow && ampRow.realCharging ? ampRow.phases : 0;
                            const slotKw = (displayAmp * 230 * slotPhases) / 1000;
                            const wbPhaseText = slotPhases > 0
                                ? ' · ' + ampRow.phases + 'p'
                                : ' · --p';
                            const wbPowerText = ' · ' + slotKw.toLocaleString('de-DE', {minimumFractionDigits: 1, maximumFractionDigits: 1}) + ' kW';
                            const wbKvaText = ampRow && ampRow.apparentKva !== null
                                ? ' · ' + ampRow.apparentKva.toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' kVA'
                                : ' · -- kVA';
                            curAmpEl.textContent = fmtAmp(displayAmp, precision) + " A" + wbPhaseText + wbPowerText + wbKvaText;
                            if (ampRow && ampRow.fineStep && ampRow.rawAmp > 0) {
                                curAmpEl.title = '0,1-A-Feinregelung aktiv: Roh-Sollstrom ' + fmtAmp(ampRow.rawAmp, 1) + ' A';
                            } else {
                                curAmpEl.removeAttribute('title');
                            }
                            const curPhaseEl = document.getElementById('wb-native-'+wb.id+'-phase');
                            if (curPhaseEl) {
                                curPhaseEl.textContent = slotPhases > 0 ? slotPhases + 'p' : '--p';
                                curPhaseEl.className = 'badge ms-1 ' + (slotPhases === 3 ? 'bg-success bg-opacity-25 text-success' : (slotPhases === 1 ? 'bg-indigo bg-opacity-25 text-indigo' : 'bg-secondary bg-opacity-25 text-secondary'));
                                curPhaseEl.style.fontSize = '0.6rem';
                                curPhaseEl.title = slotPhases > 0
                                    ? `WB${wb.id}: ${slotPhases}-phasig bestätigt aktiv`
                                    : `WB${wb.id}: Phasenwechsel- und Stop-Nachlaufwerte werden ausgeblendet`;
                            }
                        }
                        if (curStateEl) {
                            curStateEl.textContent = stateText;
                            curStateEl.title = stateReason;
                            if (rscpReason) curStateEl.title += '\n' + rscpReason;
                            curStateEl.className = stateClassMap[stateLevel] || (stateBaseText === 'Lade' ? "fw-bold text-success" : "fw-normal text-info");
                        }
                        if (curDotEl) {
                            curDotEl.style.background = stateDotMap[stateLevel] || (stateBaseText === 'Lade' ? '#22c55e' : '#22d3ee');
                            curDotEl.title = `WB${wb.id}: ${stateText || 'Idle'} - ${stateReason}`;
                            if (rscpReason) curDotEl.title += '\n' + rscpReason;
                        }
                        const priorityActive = wbPriorityMode > 0 && wbId === wbPriorityMode;
                        if (curSlotEl) {
                            curSlotEl.style.borderColor = priorityActive ? 'rgba(34,211,238,0.85)' : 'rgba(108,117,125,0.24)';
                            curSlotEl.style.background = priorityActive ? 'rgba(34,211,238,0.10)' : 'rgba(108,117,125,0.06)';
                            curSlotEl.style.boxShadow = priorityActive ? '0 0 0 1px rgba(34,211,238,0.20) inset' : 'none';
                            curSlotEl.title = priorityActive ? `WB${wb.id} hat Ladepriorität` : (wbPriorityMode ? `WB${wb.id} läuft nach Priorität` : 'Ausgeglichene Verteilung');
                        }
                        if (curPrioEl) {
                            curPrioEl.classList.toggle('d-none', !priorityActive);
                        }
                    });
                } else {
                    multiDiv.classList.add('d-none');
                }


            } catch (error) {
                if (nativeWallboxDisplayCache && (Date.now() - nativeWallboxDisplayCache.seenMs < nativeWallboxHoldMs)) {
                    return;
                }
                // Wenn Wallbox konfiguriert ist, Spalten immer sichtbar halten
                setNativeWallboxColumnsVisible(nativeWallboxEnabled);
            }
        }

        // Polling alle 5 Sekunden (analog zum Dashboard)
        if(document.getElementById('wb-native-alert')) {
            setInterval(updateNativeWallboxBanner, 5000);
            updateNativeWallboxBanner();
        }
    </script>
</body>
</html>
