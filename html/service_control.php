<?php
require_once __DIR__ . '/helpers.php';
sendNoCacheHeaders();
header('Content-Type: application/json; charset=utf-8');

$processJsonFlags = 0;
if (defined('JSON_INVALID_UTF8_SUBSTITUTE')) $processJsonFlags |= JSON_INVALID_UTF8_SUBSTITUTE;

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

function dockerServiceProcessStatusForControl($install_path, $script) {
    $python = file_exists('/opt/venv/bin/python3') ? '/opt/venv/bin/python3' : '/usr/bin/python3';
    $code = 'import os,sys; sys.path.insert(0, sys.argv[1]); import docker_healthcheck as d; '
        . 'name=sys.argv[2]; mapped=name in d.REQUIRED_PROCESSES or any(name==item[0] for item in d.OPTIONAL_PROCESSES.values()); '
        . 'matches=d._matching_processes(tuple(p for p in d._process_snapshot() if p["pid"]!=os.getpid()),name) if mapped else (); '
        . 'print("active" if any(item["state"] in d.HEALTHY_PROCESS_STATES for item in matches) else "inactive")';
    $result = e3dcRunArgvProcess([$python, '-B', '-c', $code, rtrim($install_path, '/') . '/Installer', $script],
        3.0, ['max_output_bytes' => 4096]);
    $state = trim((string)($result['stdout'] ?? ''));
    return ['available' => !empty($result['success']) && in_array($state, ['active', 'inactive'], true),
            'active' => !empty($result['success']) && $state === 'active'];
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
    "e3dc-epex-manager.service" => ["name" => "Markt- und Strompreise", "group" => "core", "desc" => "Aktualisiert dynamische Strompreisanbieter (Tibber, aWATTar, Octopus, etc.)."],
    "e3dc-weather-manager.service" => ["name" => "PV-Prognose und ML", "group" => "core", "desc" => "Bezieht tagesaktuelle Solardaten und berechnet den Vorhersage-Algorithmus inkl. ML-Modell."],
    "e3dc-storage-simulator.service" => ["name" => "Storage Simulator", "group" => "core", "desc" => "Plant vorausschauend Speicher-Entladungen basierend auf Preisen und dynamischen Prognosen."],
    "e3dc-storage-manager.service" => ["name" => "Storage Manager", "group" => "core", "desc" => "Erzwingt Lade/Entlade-Korridore auf E3DC-Ebene (V4 Brain Limiter)."],
    "e3dc-websocket.service" => ["name" => "WebUI Live-Animationen", "group" => "core", "desc" => "WebSocket-Pflichtdienst; die Weboberfläche nutzt derzeit HTTP-Abfragen und verwendet ihn noch nicht."],

    "e3dc-wallbox-manager.service" => ["name" => "Wallbox Manager", "group" => "consumers", "desc" => "Native Ladungssteuerung für externe Wallboxen inkl. Phasenanschluss-Regelung."],
    "energy_manager.service" => ["name" => "Wärmepumpe Manager", "group" => "consumers", "desc" => "PV-Boost und smarte Überschuss-Verschiebung für SG-Ready Wärmepumpen."],
    "e3dc-lux-live.service" => ["name" => "Luxtronik Live", "group" => "consumers", "desc" => "WebSocket-Überwachung der Alpha-Innotec / Novelan Wärmepumpen (Live State)."],
    "e3dc-idm-live.service" => ["name" => "IDM Live", "group" => "consumers", "desc" => "Modbus-Anbindung zum Abrufen von Sensor-Werten intelligenter IDM-Wärmepumpen."],
    "e3dc-stiebel-live.service" => ["name" => "Stiebel ISG Live", "group" => "consumers", "desc" => "Read-only Modbus-/Prozessdaten für Stiebel-Eltron ISG/WPM."],
    "e3dc-dimplex-live.service" => ["name" => "Dimplex WPM Live", "group" => "consumers", "desc" => "Read-only Modbus-Livewerte für Dimplex WPM Touch / NWPM."],
    "e3dc-heizstab.service" => ["name" => "Heizstab / Shelly", "group" => "consumers", "desc" => "Speichert Verbrauchs-Bilanzen autarker Heizstäbe/Shellys."],
    "e3dc-climate-live.service" => ["name" => "Klimaanlage Live", "group" => "consumers", "desc" => "Liest Klimaanlagenverbrauch über einen eigenen Shelly-Zähler read-only."],
    "e3dc-climate-control.service" => ["name" => "Klimaanlage Status", "group" => "consumers", "desc" => "Schreibt Toshiba-Zeitprofil und Regelstatus ohne aktive Kommandos."],
    "e3dc-forecast-evidence.service" => ["name" => "PV-Prognosediagnose", "group" => "core", "desc" => "Vergleicht Prognose und abgeschlossene E3/DC-DC-Historienslots rein diagnostisch."],
    "e3dc-ha.service" => ["name" => "HA Master/Slave", "group" => "system", "desc" => "Stellt ausfallsichere Redundanz für gekoppelte HA-Installationen (Cluster Mode) her."],
    "e3dc-matter-bridge.service" => ["name" => "Matter Bridge", "group" => "integrations", "desc" => "Stellt drei lokale read-only Statusschalter ohne Anlagenbefehle für Matter bereit."],
    "e3dc-bluelink.service" => ["name" => "Hyundai/Kia Bluelink", "group" => "integrations", "desc" => "Fragt Fahrzeug-SoC von Hyundai/Kia für das automatische Lade-Planungs Profil ab."],
    "e3dc-notifier.service" => ["name" => "Zeitplanung und Langzeit-Archiv", "group" => "core", "desc" => "Ereignisgesteuerte und periodische Nachrichten an verknüpfte Endgeräte/Chatbots."],
    "e3dc-mqtt-hub.service" => ["name" => "MQTT Hub", "group" => "integrations", "desc" => "Verteilt E3DC-Control Werte per MQTT."],
    "e3dc-shadow-sync.service" => ["name" => "Shadow-Simulation", "group" => "system", "desc" => "Liest Master-Livedaten read-only und simuliert lokale Entscheidungen ohne Steuerbefehle."]
];

$fallback_core_locked = [
    "e3dc-live.service",
    "e3dc-epex-manager.service",
    "e3dc-weather-manager.service",
    "e3dc-storage-simulator.service",
    "e3dc-storage-manager.service",
    "e3dc-notifier.service",
    "e3dc-websocket.service",
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
    "e3dc-weather-manager.service"  => ["/var/www/html/ramdisk/pv_forecast.json",           9000],
    "e3dc-storage-simulator.service"=> ["/var/www/html/ramdisk/storage_plan.json",         1200],
    "e3dc-storage-manager.service"  => ["/var/www/html/ramdisk/storage_manager_state.json", 300],
    "e3dc-wallbox-manager.service"  => ["/var/www/html/ramdisk/wallbox_native.json",        120],
    "energy_manager.service"        => ["/var/www/html/ramdisk/luxtronik.json",           120],
    "e3dc-lux-live.service"         => ["/var/www/html/ramdisk/waermepumpe.json",             120],
    "e3dc-idm-live.service"         => ["/var/www/html/ramdisk/waermepumpe.json",           120],
    "e3dc-stiebel-live.service"     => ["/var/www/html/ramdisk/stiebel_isg.json",           120],
    "e3dc-dimplex-live.service"     => ["/var/www/html/ramdisk/dimplex_wpm.json",           120],
    "e3dc-heizstab.service"         => ["/var/www/html/ramdisk/heizstab_data.json",         120],
    "e3dc-climate-live.service"     => ["/var/www/html/ramdisk/climate_load.json",          120],
    "e3dc-climate-control.service"  => ["/var/www/html/ramdisk/climate_control.json",       180],
    "e3dc-forecast-evidence.service"=> ["/var/www/html/ramdisk/pv_forecast_diagnostic_summary.json", 3600],
    "e3dc-ha.service"               => ["/var/www/html/ramdisk/ha_status.json",             120],
    "e3dc-matter-bridge.service"    => ["/var/www/html/ramdisk/matter_bridge_status.json",  120],
    "e3dc-bluelink.service"         => ["/var/www/html/ramdisk/vehicles.json",             1800],
    "e3dc-notifier.service"         => ["/var/www/html/logs/notification_manager.log",      3700],
    "e3dc-mqtt-hub.service"         => ["/var/www/html/ramdisk/mqtt_hub_status.json",          120],
    "e3dc-shadow-sync.service"      => ["/var/www/html/ramdisk/shadow_sync_status.json",    30],
];
if (!empty($catalog_control['alive'])) {
    $docker_alive_files = array_merge($docker_alive_files, $catalog_control['alive']);
}

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
    "e3dc-climate-live.service"       => ["climate_live.py", "climate_live.log"],
    "e3dc-climate-control.service"    => ["climate_control.py", "climate_control.log"],
    "e3dc-heizstab.service"          => ["heizstab_manager.py",                         "heizstab_manager.log"],
    "e3dc-ha.service"                => ["ha_manager.py",                               "ha_manager.log"],
    "e3dc-matter-bridge.service"     => ["npm run start",                               "matter_bridge.log", "npm", "matter", "matter_bridge.js"],
    "e3dc-bluelink.service"          => ["bluelink_client.py",                          "bluelink_client.log"],
    "e3dc-notifier.service"          => ["notification_manager.py",                     "notification_manager.log"],
    "e3dc-mqtt-hub.service"          => ["e3dc_mqtt_hub.py",                            "e3dc_mqtt_hub.log"],
];

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
            $exists  = isset($docker_service_map[$srv]) || $active;
            $failed  = false;
            $enabled = $active;
            $enabled_known = false;
            $enabled_raw = $active ? 'container-active' : 'container-inactive';
            $raw_status = $active ? 'active (docker)' : 'inactive (docker)';
            if (!$alive_info && isset($docker_service_map[$srv])) {
                $process = dockerServiceProcessStatusForControl($install_path, $docker_service_map[$srv][0]);
                $active = $process['active'];
                $enabled = $active;
                $enabled_raw = $active ? 'container-active' : 'container-inactive';
                $raw_status = !$process['available'] ? 'unknown (docker-process-check-unavailable)'
                    : ($active ? 'active (docker-process-check)' : 'inactive (docker-process-check)');
            }
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
    echo json_encode(["success" => true, "services" => $result, "is_docker" => $is_docker], $processJsonFlags);
    exit;
}


if ($isServiceMutation) {
    if (!isset($allowed_services[$service])) {
        echo json_encode(["success" => false, "error" => "Unerlaubter Dienst"]);
        exit;
    }

    $guard = e3dcServiceActionGuard($action, $service, $allowed_services);
    if ($guard !== null) {
        echo json_encode($guard, JSON_UNESCAPED_UNICODE);
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
        echo json_encode(e3dcActivateForecastEvidenceService(), JSON_UNESCAPED_UNICODE | $processJsonFlags);
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
        ], $processJsonFlags);
        exit;
    }

    echo json_encode(e3dcControlSystemdService($action, $service, $allowed_services), JSON_UNESCAPED_UNICODE | $processJsonFlags);
    exit;
}

echo json_encode(["success" => false, "error" => "Ungültige Aktion"]);
