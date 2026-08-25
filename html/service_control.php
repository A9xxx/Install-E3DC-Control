<?php
require_once __DIR__ . '/helpers.php';
sendNoCacheHeaders();
header('Content-Type: application/json; charset=utf-8');

$action = (string)($_GET['action'] ?? $_POST['action'] ?? '');
$serviceMutationActions = ['start', 'stop', 'restart', 'enable', 'disable', 'activate_forecast_evidence'];
$isServiceMutation = in_array($action, $serviceMutationActions, true);
if ($isServiceMutation) {
    e3dcRequirePostMutation(true);
} else {
    requireWebAuth(true);
}

$paths = getInstallPaths();
if (empty($paths['valid'])) {
    http_response_code(503);
    echo json_encode(['success' => false, 'error' => $paths['error'] ?? 'Installationskontext fehlt.']);
    exit;
}
$install_path = rtrim($paths['install_path'], '/');
// Privilegierte Dienstaktionen laufen ausschließlich über den root-eigenen Launcher.
$wrapper_path = e3dcFindServiceWrapper();

function loadServiceCatalogForControl($install_path) {
    $installer_path = rtrim($install_path, '/') . '/Installer';
    $catalog_py = $installer_path . '/service_catalog.py';
    $python = file_exists('/opt/venv/bin/python3') ? '/opt/venv/bin/python3' : '/usr/bin/python3';
    if (!file_exists($catalog_py)) {
        return ['services' => [], 'alive' => []];
    }
    $cmd = escapeshellarg($python) . ' ' . escapeshellarg($catalog_py) . ' 2>/dev/null';
    $raw = shell_exec($cmd);
    $catalog = json_decode($raw ?: '', true);
    if (!is_array($catalog)) {
        return ['services' => [], 'alive' => []];
    }
    $services = [];
    $alive = [];
    foreach ($catalog as $key => $module) {
        $unit = $module['service_unit'] ?? '';
        if ($unit === '') continue;
        $services[$unit] = [
            'name' => $module['display_name'] ?? $key,
            'group' => ($module['group'] ?? '') === 'core' ? 'core' : 'ext',
            'optional' => !empty($module['optional']),
            'desc' => $module['description'] ?? '',
            'module_key' => $key,
        ];
        if (!empty($module['alive_file'])) {
            $alive[$unit] = [
                $module['alive_file'],
                intval($module['alive_max_age_s'] ?? 120),
            ];
        }
    }
    return ['services' => $services, 'alive' => $alive];
}

function serviceUnitExistsForControl($service) {
    foreach (['/etc/systemd/system', '/lib/systemd/system', '/usr/lib/systemd/system'] as $base) {
        if (file_exists($base . '/' . $service)) {
            return true;
        }
    }
    return false;
}

// Fallback-Liste, falls der neue zentrale Katalog noch nicht verfuegbar ist.
$fallback_allowed_services = [
    "e3dc-live.service" => ["name" => "E3DC Live Data (RSCP)", "group" => "core", "desc" => "Liest sekündlich alle Live-Daten vom E3DC-Speicher über das RSCP-Protokoll aus."],
    "e3dc-epex-manager.service" => ["name" => "Markt- & Strompreise", "group" => "core", "desc" => "Aktualisiert dynamische Strompreisanbieter (Tibber, aWATTar, Octopus, etc.)."],
    "e3dc-weather-manager.service" => ["name" => "PV Prognose & ML", "group" => "core", "desc" => "Bezieht tagesaktuelle Solardaten und berechnet den Vorhersage-Algorithmus inkl. ML-Modell."],
    "e3dc-storage-simulator.service" => ["name" => "Batterie-Simulator", "group" => "core", "desc" => "Plant vorausschauend Speicher-Entladungen basierend auf Preisen und dynamischen Prognosen."],
    "e3dc-storage-manager.service" => ["name" => "V4 Speicher-Algorithmus", "group" => "core", "desc" => "Erzwingt Lade/Entlade-Korridore auf E3DC-Ebene (V4 Brain Limiter)."],
    "e3dc-websocket.service" => ["name" => "WebUI Live-Animationen", "group" => "core", "desc" => "Liefert WebSockets für das flüssige Live-Dashboard in Echtzeit-Ansicht (V4 UI)."],

    "e3dc-wallbox-manager.service" => ["name" => "Wallbox Steuerung", "group" => "ext", "desc" => "Native Ladungssteuerung für externe Wallboxen inkl. Phasenanschluss-Regelung."],
    "energy_manager.service" => ["name" => "Wärmepumpe Manager", "group" => "ext", "desc" => "PV-Boost und smarte Überschuss-Verschiebung für SG-Ready Wärmepumpen."],
    "e3dc-lux-live.service" => ["name" => "Luxtronik Daten", "group" => "ext", "desc" => "WebSocket-Überwachung der Alpha-Innotec / Novelan Wärmepumpen (Live State)."],
    "e3dc-idm-live.service" => ["name" => "IDM Wärmepumpen Daten", "group" => "ext", "desc" => "Modbus-Anbindung zum Abrufen von Sensor-Werten intelligenter IDM-Wärmepumpen."],
    "e3dc-stiebel-live.service" => ["name" => "Stiebel ISG Live", "group" => "ext", "desc" => "Read-only Modbus-/Prozessdaten für Stiebel-Eltron ISG/WPM."],
    "e3dc-dimplex-live.service" => ["name" => "Dimplex WPM Live", "group" => "ext", "desc" => "Read-only Modbus-Livewerte für Dimplex WPM Touch / NWPM."],
    "e3dc-heizstab.service" => ["name" => "Heizstab Logger", "group" => "ext", "desc" => "Speichert Verbrauchs-Bilanzen autarker Heizstäbe/Shellys."],
    "e3dc-climate-live.service" => ["name" => "Klimaanlage Live", "group" => "ext", "desc" => "Liest Klimaanlagenverbrauch über einen eigenen Shelly-Zähler read-only."],
    "e3dc-climate-control.service" => ["name" => "Klimaanlage Regel-Vorbereitung", "group" => "ext", "desc" => "Schreibt Toshiba-Zeitprofil und Regelstatus ohne aktive Kommandos."],
    "e3dc-forecast-evidence.service" => ["name" => "PV-Prognosediagnose", "group" => "ext", "desc" => "Vergleicht Prognose und abgeschlossene E3/DC-DC-Historienslots rein diagnostisch."],
    "e3dc-ha.service" => ["name" => "Home Assistant Bridge", "group" => "ext", "desc" => "Stellt ausfallsichere Redundanz für gekoppelte HA-Installationen (Cluster Mode) her."],
    "e3dc-matter-bridge.service" => ["name" => "Apple Home / Matter", "group" => "ext", "desc" => "Stellt drei lokale read-only Statusschalter ohne Anlagenbefehle für Matter bereit."],
    "e3dc-bluelink.service" => ["name" => "Hyundai/Kia Bluelink", "group" => "ext", "desc" => "Fragt Fahrzeug-SoC von Hyundai/Kia für das automatische Lade-Planungs Profil ab."],
    "e3dc-notifier.service" => ["name" => "Push & Telegram Meldungen", "group" => "ext", "desc" => "Ereignisgesteuerte und periodische Nachrichten an verknüpfte Endgeräte/Chatbots."],
    "e3dc-mqtt-hub.service" => ["name" => "MQTT Hub", "group" => "ext", "desc" => "Verteilt E3DC-Control Werte per MQTT."],
    "e3dc-shadow-sync.service" => ["name" => "Shadow-Simulation", "group" => "ext", "desc" => "Liest Master-Livedaten read-only und simuliert lokale Entscheidungen ohne Steuerbefehle."]
];

$fallback_core_locked = [
    "e3dc-live.service",
    "e3dc-epex-manager.service",
    "e3dc-weather-manager.service",
    "e3dc-storage-simulator.service",
    "e3dc-storage-manager.service",
];
foreach ($fallback_allowed_services as $fallback_unit => &$fallback_info) {
    $fallback_info['optional'] = !in_array($fallback_unit, $fallback_core_locked, true);
}
unset($fallback_info);

$catalog_control = loadServiceCatalogForControl($install_path);
$allowed_services = !empty($catalog_control['services']) ? $catalog_control['services'] : $fallback_allowed_services;

$service = $isServiceMutation
    ? (string)($_POST['service'] ?? '')
    : (string)($_GET['service'] ?? $_POST['service'] ?? '');

// Sichert ab, ob der Wrapper existiert
$wrapper_path = ($wrapper_path && file_exists($wrapper_path)) ? $wrapper_path : '';

// Docker-Erkennung und Alive-Files global definiert (genutzt in status_all UND action-Handler)
$is_docker = file_exists('/.dockerenv') ||
             (trim((string)shell_exec("docker ps --format '{{.Names}}' 2>/dev/null | grep -c e3dc-control")) > 0);

// Ramdisk Alive-Indicator pro Dienst (Docker + Bare-Metal Fallback)
// Alter in Sekunden: Datei muss juenger als dieser Wert sein um als "aktiv" zu gelten.
$docker_alive_files = [
    "e3dc-live.service"             => ["/var/www/html/ramdisk/live_data_py.json",          30],
    "e3dc-epex-manager.service"     => ["/var/www/html/ramdisk/epex_daten.json",            3700],
    "e3dc-weather-manager.service"  => ["/var/www/html/ramdisk/pv_forecast.json",           7200],
    "e3dc-storage-simulator.service"=> ["/var/www/html/ramdisk/storage_plan.json",          900],
    "e3dc-storage-manager.service"  => ["/var/www/html/ramdisk/storage_manager_state.json", 300],
    "e3dc-websocket.service"        => ["/var/www/html/ramdisk/live_data_py.json",          30],
    "e3dc-wallbox-manager.service"  => ["/var/www/html/ramdisk/wallbox_native.json",        120],
    "energy_manager.service"        => ["/var/www/html/ramdisk/waermepumpe.json",           120],
    "e3dc-lux-live.service"         => ["/var/www/html/ramdisk/luxtronik.json",             120],
    "e3dc-idm-live.service"         => ["/var/www/html/ramdisk/waermepumpe.json",           120],
    "e3dc-stiebel-live.service"     => ["/var/www/html/ramdisk/stiebel_isg.json",           120],
    "e3dc-dimplex-live.service"     => ["/var/www/html/ramdisk/dimplex_wpm.json",           120],
    "e3dc-heizstab.service"         => ["/var/www/html/ramdisk/heizstab_data.json",         120],
    "e3dc-climate-live.service"     => ["/var/www/html/ramdisk/climate_load.json",          120],
    "e3dc-climate-control.service"  => ["/var/www/html/ramdisk/climate_control.json",       180],
    "e3dc-forecast-evidence.service"=> ["/var/www/html/ramdisk/pv_forecast_diagnostic_summary.json", 3600],
    "e3dc-ha.service"               => ["/var/www/html/ramdisk/ha_status.json",             120],
    "e3dc-matter-bridge.service"    => ["/var/www/html/ramdisk/matter_pairing.json",        300],
    "e3dc-bluelink.service"         => ["/var/www/html/ramdisk/vehicles.json",              600],
    "e3dc-notifier.service"         => ["/var/www/html/logs/notification_manager.log",      3700],
    "e3dc-mqtt-hub.service"         => ["/var/www/html/logs/e3dc_mqtt_hub.log",             3700],
    "e3dc-shadow-sync.service"      => ["/var/www/html/ramdisk/shadow_sync_status.json",    30],
];
if (!empty($catalog_control['alive'])) {
    $docker_alive_files = array_merge($docker_alive_files, $catalog_control['alive']);
}

if ($action === 'status_all') {
    $result = [];

    foreach ($allowed_services as $srv => $info) {
        $raw_status = 'unknown';
        $active = $failed = $enabled = $exists = false;
        $enabled_known = false;
        $enabled_raw = 'unknown';

        if (!$is_docker) {
            // Bare-Metal: Erst systemctl versuchen
            $active_output = e3dcSystemdServiceProperty('is-active', $srv);
            $enabled_output = e3dcSystemdServiceProperty('is-enabled', $srv);
            // Service-Datei direkt prüfen: stabiler als systemctl show für www-data (kein D-Bus nötig)
            $exists  = serviceUnitExistsForControl($srv);

            $active  = ($active_output  === 'active');
            $failed  = ($active_output  === 'failed');
            $enabled = ($enabled_output === 'enabled' || $enabled_output === 'static');
            $enabled_known = in_array($enabled_output, ['enabled', 'disabled', 'static'], true);
            $enabled_raw = $enabled_output !== '' ? $enabled_output : 'unknown';
            $raw_status = $active_output ?: 'unknown';

            // Fallback: Wenn systemctl leere Ausgabe liefert (www-data kein D-Bus Zugriff),
            // prüfen wir ob die Ramdisk-Ausgabedatei frisch ist.
            if ($active_output === '' && isset($docker_alive_files[$srv])) {
                $fpath   = $docker_alive_files[$srv][0];
                $max_age = $docker_alive_files[$srv][1];
                if (file_exists($fpath) && (time() - filemtime($fpath)) < $max_age) {
                    $active     = true;
                    $exists     = true;  // Dienst produziert Output -> existiert definitiv
                    $raw_status = 'active (file-check)';
                } elseif (file_exists($fpath)) {
                    // Datei zu alt -> Dienst existiert aber ist inaktiv
                    $exists     = true;
                    $active     = false;
                    $raw_status = 'inactive (file-stale)';
                }
            }

        } else {
            // Docker-Modus: Pruefe ob die zugehoerige Ramdisk/Log-Datei frisch ist
            $alive_info = isset($docker_alive_files[$srv]) ? $docker_alive_files[$srv] : null;
            if ($alive_info) {
                $fpath   = $alive_info[0];
                $max_age = $alive_info[1];
                if (file_exists($fpath) && (time() - filemtime($fpath)) < $max_age) {
                    $active = true;
                }
            }
            $exists  = $active;
            $failed  = false;
            $enabled = $active;
            $enabled_known = false;
            $enabled_raw = $active ? 'container-active' : 'container-inactive';
            $raw_status = $active ? 'active (docker)' : 'inactive (docker)';
        }

        $result[$srv] = [
            'name'       => $info['name'],
            'group'      => $info['group'],
            'optional'   => $info['optional'] ?? true,
            'desc'       => $info['desc'],
            'exists'     => $exists,
            'active'     => $active,
            'failed'     => $failed,
            'enabled'    => $enabled,
            'enabled_known' => $enabled_known,
            'enabled_raw' => $enabled_raw,
            'raw_status' => $raw_status,
            'is_docker'  => $is_docker,
        ];
    }
    echo json_encode(["success" => true, "services" => $result, "is_docker" => $is_docker]);
    exit;
}


if ($isServiceMutation) {
    if (!isset($allowed_services[$service])) {
        echo json_encode(["success" => false, "error" => "Unerlaubter Dienst"]);
        exit;
    }

    $service_info = $allowed_services[$service] ?? [];
    $is_core_service = (($service_info['group'] ?? '') === 'core') && empty($service_info['optional']);
    if ($is_core_service && !in_array($action, ['restart'], true)) {
        echo json_encode([
            "success" => false,
            "error_code" => "core_action_blocked",
            "error" => "Kern-Dienst geschützt.",
            "message" => "Core-Dienste dürfen in der Installationszentrale nur neu gestartet und diagnostiziert werden.",
            "hint" => "Bitte Neustart oder Diagnose verwenden. Stop, Start, Enable, Disable und Rückbau bleiben für Core-Dienste gesperrt.",
            "service" => $service,
        ]);
        exit;
    }

    if ($action === 'activate_forecast_evidence') {
        if ($service !== 'e3dc-forecast-evidence.service') {
            http_response_code(400);
            echo json_encode([
                "success" => false,
                "error_code" => "forecast_evidence_target_rejected",
                "error" => "Diese gebundene Aktion ist ausschließlich für die PV-Prognosediagnose zulässig.",
                "service" => $service,
            ]);
            exit;
        }
        echo json_encode(e3dcActivateForecastEvidenceService(), JSON_UNESCAPED_UNICODE);
        exit;
    }

    if ($service === 'e3dc-forecast-evidence.service') {
        http_response_code(409);
        echo json_encode([
            "success" => false,
            "error_code" => "forecast_evidence_transaction_required",
            "error" => "Die PV-Prognosediagnose darf nur über ihre gebundene Aktivierungstransaktion geändert werden.",
            "message" => "Bitte ausschließlich die Aktion „Aktivieren & starten“ verwenden; generische Einzelaktionen bleiben für diesen Dienst gesperrt.",
            "service" => $service,
        ]);
        exit;
    }

    // Docker-Erkennung (gleiche Logik wie oben)
    $is_docker_action = file_exists('/.dockerenv');

    if ($is_docker_action) {
        // Docker: kein systemctl vorhanden — direkte Prozess-Steuerung via pkill + nohup
        // Mapping: Service-Name -> [Python-Skript relativ zu /app/pi/Install/Installer, Log-Datei]
        $docker_service_map = [
            "e3dc-live.service"              => ["e3dc_live.py --write --loops 0 --interval 3", "e3dc_live.log"],
            "e3dc-epex-manager.service"      => ["epex_manager.py",                             "epex_manager.log"],
            "e3dc-weather-manager.service"   => ["Forecast/pv_forecast_service.py",             "pv_forecast.log"],
            "e3dc-storage-simulator.service" => ["storage_simulator.py",                        "storage_simulator.log"],
            "e3dc-storage-manager.service"   => ["storage_manager.py",                          "storage_manager.log"],
            "e3dc-websocket.service"         => ["e3dc_websocket.py",                           "e3dc_websocket.log"],
            "e3dc-wallbox-manager.service"   => ["wallbox_manager.py",                          "wallbox_manager.log"],
            "energy_manager.service"         => ["luxtronik/energy_manager.py",                 "energy_manager.log"],
            "e3dc-lux-live.service"          => ["luxtronik/lux_live.py",                       "lux_live.log"],
            "e3dc-idm-live.service"          => ["idm/idm_live.py",                             "idm_live.log"],
            "e3dc-stiebel-live.service"      => ["stiebel/stiebel_live.py",                     "stiebel_live.log"],
            "e3dc-dimplex-live.service"      => ["dimplex/dimplex_live.py",                     "dimplex_live.log"],
            "e3dc-heizstab.service"          => ["heizstab_manager.py",                         "heizstab_manager.log"],
            "e3dc-ha.service"                => ["ha_manager.py",                               "ha_manager.log"],
            "e3dc-matter-bridge.service"     => ["npm run start",                               "matter_bridge.log", "npm", "matter", "matter_bridge.js"],
            "e3dc-bluelink.service"          => ["bluelink_client.py",                          "bluelink_client.log"],
            "e3dc-notifier.service"          => ["notification_manager.py",                     "notification_manager.log"],
            "e3dc-mqtt-hub.service"          => ["e3dc_mqtt_hub.py",                            "e3dc_mqtt_hub.log"],
        ];

        if (!isset($docker_service_map[$service])) {
            echo json_encode(["success" => false, "error" => "Kein Docker-Mapping für diesen Dienst"]);
            exit;
        }

        $script   = $docker_service_map[$service][0];
        $logfile  = "/var/www/html/logs/" . $docker_service_map[$service][1];
        $runner   = $docker_service_map[$service][2] ?? 'python';
        $workdir  = "/app/pi/Install/Installer";
        if (!empty($docker_service_map[$service][3])) {
            $workdir .= "/" . trim($docker_service_map[$service][3], "/");
        }
        $kill_pattern = $docker_service_map[$service][4] ?? explode(' ', $script)[0];
        $py       = "/opt/venv/bin/python3";

        $pgrep = is_executable('/usr/bin/pgrep') ? '/usr/bin/pgrep' : '/bin/pgrep';
        $pkill = is_executable('/usr/bin/pkill') ? '/usr/bin/pkill' : '/bin/pkill';
        $alive_file = $docker_alive_files[$service][0] ?? null;
        $alive_age = $docker_alive_files[$service][1] ?? 120;
        $alive_before_mtime = $alive_file && is_file($alive_file) ? (int)@filemtime($alive_file) : 0;
        $before_probe = is_executable($pgrep)
            ? e3dcRunArgvProcess([$pgrep, '-f', $kill_pattern], 5.0, ['max_output_bytes' => 8192])
            : ['success' => false, 'stdout' => ''];
        $before_pids = preg_split('/\s+/', trim((string)($before_probe['stdout'] ?? '')), -1, PREG_SPLIT_NO_EMPTY) ?: [];
        sort($before_pids, SORT_STRING);
        $was_running = !empty($before_probe['success']) && $before_pids !== [];

        $out = "";
        $stop_ok = true;
        $start_ok = true;
        $noop = in_array($action, ['start', 'enable'], true) && $was_running;
        if (in_array($action, ['stop', 'restart'])) {
            $stop_result = is_executable($pkill)
                ? e3dcRunArgvProcess([$pkill, '-f', $kill_pattern], 5.0, ['max_output_bytes' => 8192])
                : ['exit_code' => 127, 'stderr' => 'pkill fehlt'];
            $stop_ok = in_array((int)($stop_result['exit_code'] ?? 1), [0, 1], true);
            $kill_out = trim((string)($stop_result['stdout'] ?? '') . "\n" . (string)($stop_result['stderr'] ?? ''));
            $out .= "pkill: " . ($kill_out !== '' ? $kill_out : ($stop_ok ? "OK" : "fehlgeschlagen")) . "\n";
            sleep(1);
        }
        if (in_array($action, ['start', 'restart', 'enable']) && !$noop && $stop_ok) {
            if ($runner === 'npm') {
                $start_cmd = "cd " . escapeshellarg($workdir) . " && nohup npm run start"
                           . " >> " . escapeshellarg($logfile) . " 2>&1 &";
            } else {
                $start_cmd = "cd " . escapeshellarg($workdir) . " && nohup "
                           . escapeshellarg($py) . " " . $script
                           . " >> " . escapeshellarg($logfile) . " 2>&1 &";
            }
            $start_result = is_executable('/bin/sh')
                ? e3dcRunArgvProcess(['/bin/sh', '-c', $start_cmd], 5.0, ['max_output_bytes' => 8192])
                : ['success' => false];
            $start_ok = !empty($start_result['success']);
            $out .= ($start_ok ? "Gestartet: " : "Start fehlgeschlagen: ") . "$script\n";
        } elseif ($noop) {
            $out .= "Bereits aktiv; kein zweiter Prozess gestartet.\n";
        }
        if ($action === 'disable') {
            $stop_result = is_executable($pkill)
                ? e3dcRunArgvProcess([$pkill, '-f', $kill_pattern], 5.0, ['max_output_bytes' => 8192])
                : ['exit_code' => 127, 'stderr' => 'pkill fehlt'];
            $stop_ok = in_array((int)($stop_result['exit_code'] ?? 1), [0, 1], true);
            $kill_out = trim((string)($stop_result['stdout'] ?? '') . "\n" . (string)($stop_result['stderr'] ?? ''));
            $out .= "Gestoppt: " . ($kill_out !== '' ? $kill_out : ($stop_ok ? "OK" : "fehlgeschlagen")) . "\n";
        }

        // Kurz warten, dann Prozess-/Alive-Check
        sleep(2);
        $alive_after_mtime = $alive_file && is_file($alive_file) ? (int)@filemtime($alive_file) : 0;
        $alive_fresh = $alive_after_mtime > 0 && (time() - $alive_after_mtime) < $alive_age;
        $alive_advanced = $alive_fresh && $alive_after_mtime > $alive_before_mtime;
        $after_probe = is_executable($pgrep)
            ? e3dcRunArgvProcess([$pgrep, '-f', $kill_pattern], 5.0, ['max_output_bytes' => 8192])
            : ['success' => false, 'stdout' => ''];
        $after_pids = preg_split('/\s+/', trim((string)($after_probe['stdout'] ?? '')), -1, PREG_SPLIT_NO_EMPTY) ?: [];
        sort($after_pids, SORT_STRING);
        $process_running = !empty($after_probe['success']) && $after_pids !== [];
        $process_replaced = $process_running && $after_pids !== $before_pids;
        $is_active = $process_running || $alive_fresh;
        $action_success = true;
        if ($action === 'restart') {
            $action_success = $stop_ok && $start_ok && ($process_replaced || $alive_advanced);
        } elseif (in_array($action, ['start', 'enable'], true)) {
            $action_success = $noop || ($start_ok && ($process_running || $alive_advanced));
        } elseif (in_array($action, ['stop', 'disable'], true)) {
            $action_success = $stop_ok && !$process_running;
            $is_active = $process_running ? true : false;
        }

        if (!$action_success) http_response_code(500);

        echo json_encode([
            "success" => $action_success,
            "output"  => $out,
            "status"  => $is_active ? "active (docker)" : "inactive (docker)",
            "enabled" => $is_active,
            "is_docker" => true,
            "noop" => $noop,
            "message" => $action_success
                ? "Docker-Dienstaktion abgeschlossen."
                : "Docker-Dienstaktion wurde ausgeführt, aber der erwartete Zielzustand wurde nicht erreicht.",
        ]);
        exit;
    }

    // Bare-Metal: Fehlende Units laienverstaendlich melden, bevor systemctl roh scheitert.
    if (!$is_docker_action && !serviceUnitExistsForControl($service) && in_array($action, ['start', 'restart', 'enable'])) {
        $module_name = $allowed_services[$service]['name'] ?? $service;
        echo json_encode([
            "success" => false,
            "error_code" => "unit_missing",
            "error" => "Dienst ist noch nicht installiert.",
            "message" => "$module_name ist im Katalog bekannt, aber die systemd-Service-Datei fehlt noch.",
            "hint" => "Bitte zuerst den Installations-Dry-Run bzw. Job-Test prüfen. Echte Installation bleibt bis zur separaten Freigabe gesperrt.",
            "service" => $service,
            "status" => "missing",
            "enabled" => false,
        ]);
        exit;
    }

    if (!$is_docker_action && serviceUnitExistsForControl($service)) {
        $current_active = e3dcSystemdServiceProperty('is-active', $service) === 'active';
        if ($action === 'start' && $current_active) {
            echo json_encode([
                "success" => true,
                "noop" => true,
                "message" => "Dienst läuft bereits. Start ist nicht nötig.",
                "service" => $service,
                "status" => "active",
                "enabled" => e3dcSystemdServiceProperty('is-enabled', $service) === 'enabled',
            ]);
            exit;
        }
        if ($action === 'stop' && !$current_active) {
            echo json_encode([
                "success" => true,
                "noop" => true,
                "message" => "Dienst ist bereits gestoppt. Stop ist nicht nötig.",
                "service" => $service,
                "status" => "inactive",
                "enabled" => e3dcSystemdServiceProperty('is-enabled', $service) === 'enabled',
            ]);
            exit;
        }
    }

    // Bare-Metal: nur der Wrapper im validierten Release-Root ist erlaubt.
    if ($wrapper_path === '') {
        http_response_code(503);
        echo json_encode(['success' => false, 'error' => 'Service-Wrapper im Installationspfad nicht gefunden.']);
        exit;
    }
    $wrapper_result = e3dcRunServiceWrapperAction($action, [$service]);
    $output = (string)($wrapper_result['output'] ?? '');
    if (!empty($wrapper_result['errors'])) {
        $output = trim($output . "\n" . implode("\n", $wrapper_result['errors']));
    }

    // Nach Ausführung Status direkt prüfen
    $active_output = e3dcSystemdServiceProperty('is-active', $service);
    $enabled_output = e3dcSystemdServiceProperty('is-enabled', $service);

    $action_success = !empty($wrapper_result['success']);
    if (in_array($action, ['start', 'restart'], true)) {
        $action_success = $action_success && ($active_output === 'active');
    } elseif ($action === 'stop') {
        $action_success = $action_success && ($active_output !== 'active');
    } elseif ($action === 'enable') {
        $action_success = $action_success
            && ($enabled_output === 'enabled' || $enabled_output === 'static');
    } elseif ($action === 'disable') {
        $action_success = $action_success
            && !($enabled_output === 'enabled' || $enabled_output === 'static');
    }

    echo json_encode([
        "success" => $action_success,
        "output"  => $output,
        "status"  => $active_output,
        "enabled" => ($enabled_output === 'enabled' || $enabled_output === 'static'),
        "message" => $action_success
            ? "Dienstaktion abgeschlossen."
            : "Dienstaktion wurde ausgeführt, aber der erwartete Zielzustand wurde nicht erreicht."
    ]);
    exit;
}

echo json_encode(["success" => false, "error" => "Ungültige Aktion"]);
