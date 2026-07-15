<?php
require_once __DIR__ . '/helpers.php';
sendNoCacheHeaders();
requireWebAuth(true);
header('Content-Type: application/json; charset=utf-8');

// Lese e3dc_paths für Install_User Pfade (Wrapper liegt im Setup-Verzeichnis)
$paths_json = @file_get_contents('/var/www/html/e3dc_paths.json');
$paths = $paths_json ? json_decode($paths_json, true) : null;
$install_path = isset($paths['install_path']) ? rtrim($paths['install_path'], '/') : '/home/pi/Install';
// Wrapper liegt im Installer-Ordner des konfigurierten Installationspfads.
$wrapper_path = $install_path . '/Installer/service_wrapper.sh';

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
    "e3dc-climate-control.service" => ["name" => "Toshiba Cloud Status", "group" => "ext", "desc" => "Liest Toshiba-Cloud-Daten read-only und sendet keine Kommandos."],
    "e3dc-ha.service" => ["name" => "Home Assistant Bridge", "group" => "ext", "desc" => "Stellt ausfallsichere Redundanz für gekoppelte HA-Installationen (Cluster Mode) her."],
    "e3dc-matter-bridge.service" => ["name" => "Apple Home / Matter", "group" => "ext", "desc" => "Stellt drei lokale read-only Statusschalter für Matter-Routinen bereit."],
    "e3dc-bluelink.service" => ["name" => "Hyundai/Kia Bluelink", "group" => "ext", "desc" => "Fragt Fahrzeug-SoC von Hyundai/Kia für das automatische Lade-Planungs Profil ab."],
    "e3dc-notifier.service" => ["name" => "Push & Telegram Meldungen", "group" => "ext", "desc" => "Ereignisgesteuerte und periodische Nachrichten an verknüpfte Endgeräte/Chatbots."],
    "e3dc-mqtt-hub.service" => ["name" => "MQTT Hub", "group" => "ext", "desc" => "Verteilt E3DC-Control Werte per MQTT."],
    "e3dc-shadow-sync.service" => ["name" => "Shadow-Vergleichsinstanz", "group" => "ext", "desc" => "Liest die aktive Instanz read-only und berechnet lokale Vergleichsentscheidungen ohne Steuerbefehle oder Failover."]
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

$action = isset($_GET['action']) ? $_GET['action'] : (isset($_POST['action']) ? $_POST['action'] : '');
$service = isset($_GET['service']) ? $_GET['service'] : (isset($_POST['service']) ? $_POST['service'] : '');

// Sichert ab, ob der Wrapper existiert
if (!file_exists($wrapper_path)) {
    // Fallback falls der Ordner anders heisst
    $wrapper_path = '/home/pi/Install/Installer/service_wrapper.sh';
}

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

        if (!$is_docker) {
            // Bare-Metal: Erst systemctl versuchen
            $active_output  = trim((string)shell_exec("systemctl is-active "  . escapeshellarg($srv) . " 2>/dev/null"));
            $enabled_output = trim((string)shell_exec("systemctl is-enabled " . escapeshellarg($srv) . " 2>/dev/null"));
            // Service-Datei direkt prüfen: stabiler als systemctl show für www-data (kein D-Bus nötig)
            $exists  = serviceUnitExistsForControl($srv);

            $active  = ($active_output  === 'active');
            $failed  = ($active_output  === 'failed');
            $enabled = ($enabled_output === 'enabled' || $enabled_output === 'static');
            $raw_status = $active_output ?: 'unknown';

            // Fallback: Wenn systemctl leere Ausgabe liefert (www-data kein D-Bus Zugriff),
            // prüfen wir ob die Ramdisk-Ausgabedatei frisch ist.
            if ($active_output === '' && isset($docker_alive_files[$srv])) {
                $fpath   = $docker_alive_files[$srv][0];
                $max_age = $docker_alive_files[$srv][1];
                if (file_exists($fpath) && (time() - filemtime($fpath)) < $max_age) {
                    $active     = true;
                    $enabled    = true;
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
            'raw_status' => $raw_status,
            'is_docker'  => $is_docker,
        ];
    }
    echo json_encode(["success" => true, "services" => $result, "is_docker" => $is_docker]);
    exit;
}


if (in_array($action, ['start', 'stop', 'restart', 'enable', 'disable'])) {
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

    // Docker-Erkennung (gleiche Logik wie oben)
    $is_docker_action = file_exists('/.dockerenv');

    if ($is_docker_action) {
        // Docker: kein systemctl vorhanden — direkte Prozess-Steuerung via pkill + nohup
        // Mapping: Service-Name -> [Python-Skript relativ zu /app/pi/Install/Installer, Log-Datei]
        $docker_service_map = [
            "e3dc-live.service"              => ["e3dc_live.py --write --loops 0 --interval 3", "e3dc_live.log"],
            "e3dc-epex-manager.service"      => ["epex_manager.py",                             "epex_manager.log"],
            "e3dc-weather-manager.service"   => ["Forecast/pv_forecast_service.py",             "weather_manager.log"],
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

        $out = "";
        if (in_array($action, ['stop', 'restart'])) {
            $kill_out = shell_exec("pkill -f " . escapeshellarg($kill_pattern) . " 2>&1");
            $out .= "pkill: " . ($kill_out ?: "OK") . "\n";
            sleep(1);
        }
        if (in_array($action, ['start', 'restart', 'enable'])) {
            if ($runner === 'npm') {
                $start_cmd = "cd " . escapeshellarg($workdir) . " && nohup npm run start"
                           . " >> " . escapeshellarg($logfile) . " 2>&1 &";
            } else {
                $start_cmd = "cd " . escapeshellarg($workdir) . " && nohup "
                           . escapeshellarg($py) . " " . $script
                           . " >> " . escapeshellarg($logfile) . " 2>&1 &";
            }
            shell_exec($start_cmd);
            $out .= "Gestartet: $script\n";
        }
        if ($action === 'disable') {
            $kill_out = shell_exec("pkill -f " . escapeshellarg($kill_pattern) . " 2>&1");
            $out .= "Gestoppt: " . ($kill_out ?: "OK") . "\n";
        }

        // Kurz warten, dann Prozess-/Alive-Check
        sleep(2);
        $alive_file = $docker_alive_files[$service][0] ?? null;
        $alive_age  = $docker_alive_files[$service][1] ?? 120;
        $alive_fresh = $alive_file && file_exists($alive_file) && (time() - filemtime($alive_file)) < $alive_age;
        $pgrep_output = trim((string)shell_exec("pgrep -f " . escapeshellarg($kill_pattern) . " 2>/dev/null"));
        $process_running = ($pgrep_output !== '');
        $is_active = $process_running || $alive_fresh;
        $action_success = true;
        if (in_array($action, ['start', 'restart', 'enable'], true)) {
            $action_success = $is_active;
        } elseif (in_array($action, ['stop', 'disable'], true)) {
            $action_success = !$process_running;
            $is_active = $process_running ? true : false;
        }

        echo json_encode([
            "success" => $action_success,
            "output"  => $out,
            "status"  => $is_active ? "active (docker)" : "inactive (docker)",
            "enabled" => $is_active,
            "is_docker" => true,
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
            "hint" => "Bitte zuerst die Read-only-Installationsprüfung ausführen. Die Installation bleibt bis zur bestätigten Wrapper-Freigabe gesperrt.",
            "service" => $service,
            "status" => "missing",
            "enabled" => false,
        ]);
        exit;
    }

    if (!$is_docker_action && serviceUnitExistsForControl($service)) {
        $current_active = trim((string)shell_exec("systemctl is-active " . escapeshellarg($service) . " 2>/dev/null")) === 'active';
        if ($action === 'start' && $current_active) {
            echo json_encode([
                "success" => true,
                "noop" => true,
                "message" => "Dienst läuft bereits. Start ist nicht nötig.",
                "service" => $service,
                "status" => "active",
                "enabled" => trim((string)shell_exec("systemctl is-enabled " . escapeshellarg($service) . " 2>/dev/null")) === 'enabled',
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
                "enabled" => trim((string)shell_exec("systemctl is-enabled " . escapeshellarg($service) . " 2>/dev/null")) === 'enabled',
            ]);
            exit;
        }
    }

    // Bare-Metal: Wrapper mit sudo
    $cmd = "sudo " . escapeshellarg($wrapper_path) . " " . escapeshellarg($action) . " " . escapeshellarg($service) . " 2>&1";
    $output = shell_exec($cmd);

    // Nach Ausführung Status direkt prüfen
    $active_output  = trim((string)shell_exec("systemctl is-active "  . escapeshellarg($service)));
    $enabled_output = trim((string)shell_exec("systemctl is-enabled " . escapeshellarg($service) . " 2>/dev/null"));

    $action_success = true;
    if (in_array($action, ['start', 'restart'], true)) {
        $action_success = ($active_output === 'active');
    } elseif ($action === 'stop') {
        $action_success = ($active_output !== 'active');
    } elseif ($action === 'enable') {
        $action_success = ($enabled_output === 'enabled' || $enabled_output === 'static');
    } elseif ($action === 'disable') {
        $action_success = !($enabled_output === 'enabled' || $enabled_output === 'static');
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
