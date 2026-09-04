<?php
require_once __DIR__ . '/helpers.php';
sendNoCacheHeaders();
requireWebAuth(false);

$paths = function_exists('getInstallPaths') ? getInstallPaths() : [];
$install_path = !empty($paths['valid']) ? rtrim($paths['install_path'], '/') : '';
if ($install_path === '' || !is_dir($install_path . '/Installer')) {
    http_response_code(503);
    echo '<!doctype html><html lang="de"><meta charset="utf-8"><title>Installationskontext fehlt</title>';
    echo '<p>Der Installationspfad ist nicht eindeutig konfiguriert. Bitte den geprüften Bootstrap-/Installerweg verwenden.</p>';
    exit;
}
$installer_path = $install_path . '/Installer';
$python = e3dcIsDockerEnvironment() && file_exists('/opt/venv/bin/python3')
    ? '/opt/venv/bin/python3'
    : '/usr/bin/python3';
$web_installer = $installer_path . '/web_installer.py';
$installer_wrapper = $installer_path . '/installer_wrapper.sh';

function installCenterCsrfToken() {
    return e3dcCsrfToken();
}

function validateInstallCenterCsrf() {
    $sent = $_POST['csrf_token'] ?? '';
    $expected = e3dcCsrfToken();
    return is_string($sent)
        && $sent !== ''
        && e3dcWebAuthHashEquals($expected, $sent);
}

function installCenterDashboardReturnUrl() {
    $requested = strtolower(trim((string)($_GET['return'] ?? $_GET['from'] ?? '')));
    if (in_array($requested, ['mobile', 'mobile.php'], true)) {
        return 'mobile.php';
    }
    if (in_array($requested, ['desktop', 'dashboard', 'index', 'index.php'], true)) {
        return 'index.php';
    }

    $referer = $_SERVER['HTTP_REFERER'] ?? '';
    if (is_string($referer) && $referer !== '') {
        $parts = parse_url($referer);
        $refHost = strtolower((string)($parts['host'] ?? ''));
        $currentHost = strtolower((string)($_SERVER['HTTP_HOST'] ?? ''));
        $currentHost = preg_replace('/:\d+$/', '', $currentHost);
        if ($refHost === '' || $currentHost === '' || $refHost === $currentHost) {
            $entry = strtolower(basename((string)($parts['path'] ?? '')));
            if ($entry === 'mobile.php') {
                return 'mobile.php';
            }
            if ($entry === 'index.php') {
                return 'index.php';
            }
        }
    }

    return 'index.php';
}

function runInstallerAction($action, $module = null) {
    global $python, $web_installer;
    if (!file_exists($web_installer)) {
        return ['success' => false, 'error' => 'web_installer.py nicht gefunden'];
    }
    $cmd = escapeshellarg($python) . ' ' . escapeshellarg($web_installer) . ' --action ' . escapeshellarg($action) . ' 2>&1';
    if ($module !== null && $module !== '') {
        $cmd = escapeshellarg($python) . ' ' . escapeshellarg($web_installer)
             . ' --action ' . escapeshellarg($action)
             . ' --module ' . escapeshellarg($module) . ' 2>&1';
    }
    $out = shell_exec($cmd);
    $json = json_decode($out ?: '', true);
    if (is_array($json)) return $json;
    return ['success' => false, 'error' => trim($out ?: 'Keine Antwort vom Web-Installer')];
}

function runInstallerJob($action, $module = null, $viaWrapper = false) {
    global $python, $web_installer;
    $allowed = [
        'catalog',
        'installer_status',
        'job_status',
        'write_readiness',
        'write_permission_plan',
        'backup_plan',
        'run_diagnosis',
        'diagnosis',
        'dry_run',
        'install_module_dry_run',
        'permissions_check',
        'repair_permissions_dry_run',
        'validate_config'
    ];
    if (!in_array($action, $allowed, true)) {
        return ['success' => false, 'error' => 'Job-Aktion ist in der WebUI-Vorstufe nicht erlaubt'];
    }
    if (!preg_match('/^[a-z_]+$/', $action)) {
        return ['success' => false, 'error' => 'Ungültige Job-Aktion'];
    }
    if ($module !== null && $module !== '' && !preg_match('/^[a-z0-9_]+$/', $module)) {
        return ['success' => false, 'error' => 'Ungültiger Modul-Key'];
    }
    if ($viaWrapper) {
        return [
            'success' => false,
            'write_blocked' => true,
            'privileged_installer_web_enabled' => false,
            'error' => 'Privilegierte Installer-Webjobs sind aus Sicherheitsgründen deaktiviert.',
            'message' => 'Read-only Prüfungen laufen ohne sudo. Rechte-Reparatur und Update nutzen den vorhandenen root-eigenen Systemjob; Modulinstallation und Rückfall benötigen eine administrative Konsole.',
        ];
    }
    if (!file_exists($web_installer)) {
        return ['success' => false, 'error' => 'web_installer.py nicht gefunden'];
    }
    $ramdisk = '/var/www/html/ramdisk';
    if (!is_dir($ramdisk) && !@mkdir($ramdisk, 02775, true)) {
        return ['success' => false, 'error' => 'Ramdisk-Verzeichnis konnte nicht angelegt werden'];
    }
    $job = [
        'action' => $action === 'diagnosis' ? 'run_diagnosis' : $action,
        'module' => ($module !== null && $module !== '') ? $module : null,
        'source' => 'install_center',
        'created_at' => date('c')
    ];
    $job_file = $ramdisk . '/web_install_jobs.json';
    if (@file_put_contents($job_file, json_encode($job, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE)) === false) {
        return ['success' => false, 'error' => 'Jobdatei konnte nicht geschrieben werden'];
    }
    @chmod($job_file, 0664);
    $cmd = escapeshellarg($python) . ' ' . escapeshellarg($web_installer) . ' --job-file 2>&1';
    $out = shell_exec($cmd);
    $json = json_decode($out ?: '', true);
    if (is_array($json)) return $json;
    return [
        'success' => false,
        'error' => trim($out ?: 'Keine Antwort vom Web-Installer-Job'),
        'via_wrapper' => false,
        'message' => 'Direkter read-only Web-Installer-Job konnte nicht ausgeführt werden.'
    ];
}

function runInstallerWriteJob($action, $module = null) {
    $allowed = ['repair_permissions', 'install_module', 'remove_module'];
    if (!in_array($action, $allowed, true)) {
        return [
            'success' => false,
            'write_blocked' => true,
            'error' => 'Schreibender Job ist in dieser Stufe nicht erlaubt'
        ];
    }
    if ($action === 'repair_permissions' && $module !== null && $module !== '') {
        return [
            'success' => false,
            'write_blocked' => true,
            'error' => 'Globale Rechte-Reparatur erwartet kein Modul'
        ];
    }
    if ($action === 'install_module' || $action === 'remove_module') {
        if ($module === null || $module === '') {
            return [
                'success' => false,
                'write_blocked' => true,
                'error' => 'Modul-Schreibaktion erwartet einen Modul-Key'
            ];
        }
        if (!preg_match('/^[a-z0-9_]+$/', $module)) {
            return [
                'success' => false,
                'write_blocked' => true,
                'error' => 'Ungültiger Modul-Key'
            ];
        }
    }
    return [
        'success' => false,
        'write_blocked' => true,
        'privileged_installer_web_enabled' => false,
        'action' => $action,
        'module' => ($module !== null && $module !== '') ? $module : null,
        'error' => 'Privilegierte Installer-Webjobs sind aus Sicherheitsgründen deaktiviert.',
        'message' => 'Der alte gemeinsame Ramdisk-/sudo-Pfad wird nicht mehr verwendet. Bitte diese administrative Änderung bis zu einem eigenen engen Launcher per Konsole ausführen.',
    ];
}

function inspectRuntimePermissionsLauncher() {
    $specs = [
        ['/usr/local/sbin/e3dc-runtime-permissions-repair', 0755, 524288],
        ['/etc/e3dc-control/runtime_permissions_contract.json', 0644, 1048576],
    ];
    foreach ($specs as [$path, $expectedMode, $maximumBytes]) {
        clearstatcache(true, $path);
        $meta = @lstat($path);
        if (!is_array($meta)
            || (($meta['mode'] ?? 0) & 0170000) !== 0100000
            || (int)($meta['nlink'] ?? 0) !== 1
            || (int)($meta['uid'] ?? -1) !== 0
            || (int)($meta['gid'] ?? -1) !== 0
            || (int)($meta['size'] ?? 0) <= 0
            || (int)($meta['size'] ?? 0) > $maximumBytes
            || (((int)($meta['mode'] ?? 0)) & 07777) !== $expectedMode) {
            return [
                'ok' => false,
                'path' => $path,
                'status' => 'missing_or_unsafe',
            ];
        }
    }
    $launcher = @file_get_contents('/usr/local/sbin/e3dc-runtime-permissions-repair');
    $contractRaw = @file_get_contents('/etc/e3dc-control/runtime_permissions_contract.json');
    $contract = is_string($contractRaw) ? json_decode($contractRaw, true) : null;
    if (!is_string($launcher)
        || strlen($launcher) < 1024
        || strlen($launcher) > 524288
        || !str_starts_with($launcher, "#!/usr/bin/python3\n")
        || substr_count($launcher, 'e3dc_runtime_permissions_v1') !== 1
        || substr_count($launcher, 'e3dc_runtime_permissions_cli_v3') !== 1
        || !is_string($contractRaw)
        || strlen($contractRaw) < 2
        || strlen($contractRaw) > 1048576
        || !is_array($contract)
        || ($contract['schema'] ?? '') !== 'e3dc_runtime_permissions_v1'
        || ($contract['launcher_feature'] ?? '') !== 'e3dc_runtime_permissions_cli_v3') {
        return ['ok' => false, 'status' => 'content_invalid'];
    }
    return ['ok' => true, 'status' => 'ok'];
}

function runRuntimePermissionsRepair($checkOnly = false, $confirmationToken = '') {
    $inspection = inspectRuntimePermissionsLauncher();
    if (empty($inspection['ok'])) {
        return [
            'success' => false,
            'error_code' => 'launcher_missing_or_unsafe',
            'message' => 'Der root-eigene Rechte-Launcher oder sein Vertrag fehlt oder ist unsicher. Bitte den vollständigen Systemabgleich verwenden.',
            'inspection' => $inspection,
        ];
    }
    $confirmationToken = strtolower(trim((string)$confirmationToken));
    if ($checkOnly && $confirmationToken !== '') {
        return [
            'success' => false,
            'error_code' => 'confirmation_mode_invalid',
            'message' => 'Nur-Lese-Prüfung und Dateilistenbestätigung dürfen nicht kombiniert werden.',
        ];
    }
    if ($confirmationToken !== ''
        && preg_match('/^[0-9a-f]{64}$/D', $confirmationToken) !== 1) {
        return [
            'success' => false,
            'error_code' => 'confirmation_token_invalid',
            'message' => 'Die Dateilistenfreigabe ist ungültig. Bitte starte die Rechtereparatur erneut.',
        ];
    }
    $argv = [
        '/usr/bin/sudo',
        '-n',
        '--',
        '/usr/local/sbin/e3dc-runtime-permissions-repair',
    ];
    if ($checkOnly) {
        $argv[] = '--check-json';
    } elseif ($confirmationToken !== '') {
        $argv[] = '--confirm-content-drift';
    }
    $options = ['max_output_bytes' => 4 * 1024 * 1024];
    if ($confirmationToken !== '') {
        // Das kurzlebige Secret bleibt aus argv, Umgebung, Prozessliste und
        // Protokoll. Nur der feste sudoers-Modus erhält es über stdin.
        $options['stdin'] = $confirmationToken . "\n";
    }
    $process = e3dcRunArgvProcess($argv, 180.0, $options);
    $exitCode = (int)($process['exit_code'] ?? 1);
    $decoded = json_decode(trim((string)($process['stdout'] ?? '')), true);
    if (!is_array($decoded)) {
        return [
            'success' => false,
            'error_code' => 'launcher_response_invalid',
            'message' => 'Der Rechte-Launcher lieferte keine gültige Abschlussbestätigung.',
            'exit_code' => $exitCode,
            'process_error' => trim((string)($process['error'] ?? '')),
        ];
    }
    $decoded['exit_code'] = $exitCode;
    if ($exitCode !== 0) {
        $decoded['success'] = false;
    }
    return $decoded;
}

function installCenterConfigField($key, $label, $type = 'text', $options = [], $help = '', $secret = false, $placeholder = '') {
    return [
        'key' => $key,
        'label' => $label,
        'type' => $type,
        'options' => $options,
        'help' => $help,
        'secret' => $secret,
        'placeholder' => $placeholder
    ];
}

function installCenterBoolOptions() {
    return [
        ['value' => '0', 'label' => 'Aus'],
        ['value' => '1', 'label' => 'Ein']
    ];
}

function installCenterModuleConfigFields($moduleKey) {
    $bool = installCenterBoolOptions();
    $wbTypes = [
        ['value' => 'none', 'label' => 'Deaktiviert'],
        ['value' => 'go-e', 'label' => 'go-eCharger'],
        ['value' => 'openwb', 'label' => 'openWB Controller'],
        ['value' => 'openwb_pro', 'label' => 'openWB Pro'],
        ['value' => 'e3dc_auto', 'label' => 'E3DC automatisch (efy / Easy / Multi)'],
        ['value' => 'e3dc_efy', 'label' => 'E3DC Wallbox efy'],
        ['value' => 'e3dc_easy_connect', 'label' => 'E3DC Easy Connect'],
        ['value' => 'e3dc_multi', 'label' => 'E3DC Multi Connect'],
        ['value' => 'e3dc', 'label' => 'E3DC-Altmodus / WBchar6'],
        ['value' => 'dummy', 'label' => 'Dummy/Test']
    ];
    $wpTypes = [
        ['value' => '-1', 'label' => 'Keine Wärmepumpe'],
        ['value' => '0', 'label' => 'Luxtronik'],
        ['value' => '1', 'label' => 'IDM Navigator 2.0'],
        ['value' => '2', 'label' => 'Heizstab / Shelly'],
        ['value' => '3', 'label' => 'Shelly Pro3EM ohne native WP'],
        ['value' => '4', 'label' => 'Stiebel Eltron ISG / WPM'],
        ['value' => '5', 'label' => 'Dimplex WPM Touch / NWPM']
    ];
    $relayOptions = [
        ['value' => '-1', 'label' => '-1 = nur messen'],
        ['value' => '0', 'label' => 'Relais 0'],
        ['value' => '1', 'label' => 'Relais 1'],
        ['value' => '2', 'label' => 'Relais 2']
    ];
    $haModes = [
        ['value' => 'off', 'label' => 'Deaktiviert'],
        ['value' => 'master', 'label' => 'Master'],
        ['value' => 'slave', 'label' => 'Slave'],
        ['value' => 'shadow', 'label' => 'Shadow (Simulation)']
    ];

    $commonHeatPump = [
        installCenterConfigField('luxtronik', 'WP-/Verbrauchslogging', 'select', $bool),
        installCenterConfigField('wp_type', 'Wärmepumpen-Typ', 'select', $wpTypes),
        installCenterConfigField('auto_mode', 'Automatik darf steuern', 'select', $bool),
    ];

    $fields = [
        'live' => [
            installCenterConfigField('server_ip', 'E3DC IP-Adresse', 'text', [], '', false, '192.0.2.50'),
            installCenterConfigField('server_port', 'RSCP Port', 'number', [], '', false, '5033'),
            installCenterConfigField('e3dc_user', 'E3DC Benutzer'),
            installCenterConfigField('e3dc_password', 'E3DC Passwort', 'password', [], 'Leer lassen = unverändert.', true),
            installCenterConfigField('aes_password', 'AES Passwort', 'password', [], 'Leer lassen = unverändert.', true),
        ],
        'weather' => [
            installCenterConfigField('forecast1', 'Forecast Solar API-Key / Quelle', 'text'),
        ],
        'wallbox' => [
            installCenterConfigField('wb_native_enable', 'Native Wallbox-Regelung', 'select', $bool),
            installCenterConfigField('wb_native_type', 'Wallbox 1 Modell / API', 'select', $wbTypes),
            installCenterConfigField('wb1_e3dc_wbchar6_compat_enable', 'WB1 E3/DC WBchar6-Regelbackend', 'select', [
                ['value' => '1', 'label' => 'Empfohlen: efy/Easy Community-Kompatibilitätsregelung'],
                ['value' => '0', 'label' => 'Nur Status (keine E3/DC-Regelbefehle)'],
            ], 'Modus und Strom laufen über WB_REQ_SET_EXTERN. Ein explizites Aus bleibt erhalten.'),
            installCenterConfigField('wb_native_ip', 'Wallbox 1 IP-Adresse', 'text', [], '', false, 'leer bei E3DC RSCP'),
            installCenterConfigField('wb1_topic_prefix', 'openWB Topic Prefix WB1', 'text', [], '', false, 'openWB/simpleAPI/chargepoint'),
            installCenterConfigField('wb_native_type2', 'Wallbox 2 Modell / API', 'select', $wbTypes, 'Ein fehlender oder leerer Altbestandswert bleibt unverändert und darf nur nach frischer, eindeutiger openWB-Erkennung als WB2 gelten. Erst „Deaktiviert“ schaltet WB2 ausdrücklich aus.'),
            installCenterConfigField('wb2_e3dc_wbchar6_compat_enable', 'WB2 E3/DC WBchar6-Regelbackend', 'select', [
                ['value' => '1', 'label' => 'Empfohlen: efy/Easy Community-Kompatibilitätsregelung'],
                ['value' => '0', 'label' => 'Nur Status (keine E3/DC-Regelbefehle)'],
            ], 'Modus und Strom laufen über WB_REQ_SET_EXTERN. Ein explizites Aus bleibt erhalten.'),
            installCenterConfigField('wb_native_ip2', 'Wallbox 2 IP-Adresse', 'text', [], '', false, 'leer bei E3DC RSCP'),
            installCenterConfigField('wb2_topic_prefix', 'openWB Topic Prefix WB2', 'text', [], '', false, 'openWB/simpleAPI/chargepoint/2'),
            installCenterConfigField('wb_native_mode', 'Priorität bei zwei Wallboxen', 'select', [
                ['value' => '0', 'label' => 'Ausgeglichen'],
                ['value' => '1', 'label' => 'Vorrang Wallbox 1'],
                ['value' => '2', 'label' => 'Vorrang Wallbox 2']
            ]),
            installCenterConfigField('wbmaxladestrom', 'Standard-Maximalstrom je Wallbox (6–32 A)', 'number', [], '', false, '16'),
            installCenterConfigField('grid_max_amps', 'Hausabsicherung (A je Phase)', 'number', [], '', false, '35'),
            installCenterConfigField('wbminsoc', 'Batterie-Reserve für Wallbox (%)', 'number', [], '', false, '45'),
        ],
        'heatpump' => array_merge($commonHeatPump, [
            installCenterConfigField('luxtronik_ip', 'Luxtronik IP-Adresse', 'text', [], '', false, '192.0.2.60'),
            installCenterConfigField('idm_ip', 'IDM IP-Adresse', 'text', [], '', false, '192.0.2.61'),
            installCenterConfigField('idm_port', 'IDM Modbus-Port', 'number', [], '', false, '502'),
            installCenterConfigField('stiebel_isg_ip', 'Stiebel ISG IP-Adresse', 'text', [], '', false, '192.0.2.70'),
            installCenterConfigField('stiebel_isg_port', 'Stiebel Modbus-Port', 'number', [], '', false, '502'),
            installCenterConfigField('stiebel_isg_power_meter_enable', 'Stiebel Leistungsmesser', 'select', $bool),
            installCenterConfigField('stiebel_isg_power_meter_ip', 'Stiebel Shelly-Zähler IP', 'text', [], '', false, '192.0.2.71'),
            installCenterConfigField('dimplex_ip', 'Dimplex IP-Adresse', 'text', [], '', false, '192.0.2.80'),
            installCenterConfigField('dimplex_port', 'Dimplex Modbus-Port', 'number', [], '', false, '502'),
            installCenterConfigField('dimplex_wpm_software', 'Dimplex WPM Software', 'text', [], '', false, 'auto / M3.21'),
            installCenterConfigField('dimplex_sg_register', 'Dimplex SG Register', 'number', [], '', false, '5167'),
            installCenterConfigField('shelly_3em_ip', 'Shelly Pro3EM IP', 'text', [], '', false, '192.0.2.90'),
            installCenterConfigField('shelly_3em_relay_id', 'Shelly Relais-ID', 'select', $relayOptions),
            installCenterConfigField('shelly_3em_enable', 'PV-Auto-Steuerung', 'select', [
                ['value' => '0', 'label' => 'Nur messen'],
                ['value' => '1', 'label' => 'PV-Auto schalten']
            ]),
            installCenterConfigField('shelly_3em_wp_min_w', 'WP Mindestleistung (W)', 'number', [], '', false, '1000'),
            installCenterConfigField('shelly_3em_wp_max_w', 'WP Nennleistung (W)', 'number', [], '', false, '3000'),
        ]),
        'lux_live' => [
            installCenterConfigField('luxtronik', 'WP-/Verbrauchslogging', 'select', $bool),
            installCenterConfigField('wp_type', 'Wärmepumpen-Typ', 'select', $wpTypes),
            installCenterConfigField('luxtronik_ip', 'Luxtronik IP-Adresse', 'text', [], '', false, '192.0.2.60'),
        ],
        'idm_live' => [
            installCenterConfigField('luxtronik', 'WP-/Verbrauchslogging', 'select', $bool),
            installCenterConfigField('wp_type', 'Wärmepumpen-Typ', 'select', $wpTypes),
            installCenterConfigField('idm_ip', 'IDM IP-Adresse', 'text', [], '', false, '192.0.2.61'),
            installCenterConfigField('idm_port', 'IDM Modbus-Port', 'number', [], '', false, '502'),
            installCenterConfigField('idm_e_total', 'IDM Energiezähler-Offset', 'number'),
        ],
        'stiebel_live' => [
            installCenterConfigField('luxtronik', 'WP-/Verbrauchslogging', 'select', $bool),
            installCenterConfigField('wp_type', 'Wärmepumpen-Typ', 'select', $wpTypes),
            installCenterConfigField('stiebel_isg_ip', 'Stiebel ISG IP-Adresse', 'text', [], '', false, '192.0.2.70'),
            installCenterConfigField('stiebel_isg_port', 'Stiebel Modbus-Port', 'number', [], '', false, '502'),
            installCenterConfigField('stiebel_isg_device_id', 'Stiebel Unit-ID', 'number', [], '', false, '1'),
            installCenterConfigField('stiebel_isg_power_heating_w', 'HZ Leistung (W)', 'number', [], '', false, '1500'),
            installCenterConfigField('stiebel_isg_power_dhw_w', 'WW Leistung (W)', 'number', [], '', false, '2500'),
            installCenterConfigField('stiebel_isg_cop_estimate', 'COP Schätzung', 'number', [], '', false, '3.0'),
            installCenterConfigField('stiebel_isg_standby_w', 'Standby (W)', 'number', [], '', false, '35'),
            installCenterConfigField('stiebel_isg_scrape_hz_enable', 'Verdichter-Hz aus Web', 'select', $bool),
            installCenterConfigField('stiebel_isg_power_meter_enable', 'Externer Leistungsmesser', 'select', $bool),
            installCenterConfigField('stiebel_isg_power_meter_ip', 'Shelly-Zähler IP', 'text', [], '', false, '192.0.2.71'),
            installCenterConfigField('stiebel_isg_power_meter_type', 'Zählertyp', 'select', [
                ['value' => 'auto', 'label' => 'Auto'],
                ['value' => 'shelly_3em', 'label' => 'Shelly 3EM / Pro 3EM'],
                ['value' => 'shelly_plug', 'label' => 'Shelly Plug / Plus'],
                ['value' => 'shelly_pm', 'label' => 'Shelly PM']
            ]),
        ],
        'dimplex_live' => [
            installCenterConfigField('luxtronik', 'WP-/Verbrauchslogging', 'select', $bool),
            installCenterConfigField('wp_type', 'Wärmepumpen-Typ', 'select', $wpTypes),
            installCenterConfigField('dimplex_ip', 'Dimplex IP-Adresse', 'text', [], '', false, '192.0.2.80'),
            installCenterConfigField('dimplex_port', 'Dimplex Modbus-Port', 'number', [], '', false, '502'),
            installCenterConfigField('dimplex_unit_id', 'Dimplex Unit-ID', 'number', [], '', false, '1'),
            installCenterConfigField('dimplex_wpm_software', 'WPM Software', 'text', [], '', false, 'auto / M3.21'),
            installCenterConfigField('dimplex_sg_register', 'SG Register', 'number', [], '', false, '5167'),
            installCenterConfigField('dimplex_modbus_zero_based', '0-basierte Adresse', 'select', $bool),
            installCenterConfigField('dimplex_allow_dark_green', 'Dunkelgrün erlauben', 'select', $bool),
            installCenterConfigField('dimplex_outdoor_register', 'Außen-Register', 'number', [], '', false, '1'),
            installCenterConfigField('dimplex_dhw_register', 'WW-Ist Register', 'number', [], '', false, '3'),
            installCenterConfigField('dimplex_operating_mode_register', 'Betriebsmodus Register', 'number', [], '', false, '5015'),
            installCenterConfigField('dimplex_heat_power_register', 'Wärmeleistung Register', 'number', [], '', false, '5168'),
            installCenterConfigField('dimplex_electric_power_register', 'Elektr. Leistung Register', 'number', [], '', false, '5170'),
            installCenterConfigField('dimplex_heartbeat_out_register', 'Heartbeat Out Register', 'number', [], '', false, '5064'),
            installCenterConfigField('dimplex_temp_scale', 'Temperatur-Skalierung', 'text', [], '', false, 'auto'),
        ],
        'heizstab' => [
            installCenterConfigField('heizstab', 'Heizstab/BWWP aktiv', 'select', $bool),
            installCenterConfigField('heizstab_ip', 'my-PV / Heizstab IP', 'text', [], '', false, '192.0.2.81'),
            installCenterConfigField('heizstab_port', 'Modbus-Port', 'number', [], '', false, '502'),
            installCenterConfigField('heizstab_max_w', 'Max. Heizstableistung (W)', 'number', [], '', false, '3000'),
            installCenterConfigField('shelly_heiz_ip', 'Shelly Heizstab IP', 'text', [], '', false, '192.0.2.82'),
            installCenterConfigField('shelly_heiz_w', 'Shelly Heizleistung (W)', 'number', [], '', false, '1500'),
            installCenterConfigField('hs_auto_mode', 'Heizstab Auto-Modus', 'select', $bool),
            installCenterConfigField('hs_min_surplus_w', 'Min. PV-Überschuss (W)', 'number'),
            installCenterConfigField('hs_min_soc', 'Min. Speicher-SoC (%)', 'number'),
        ],
        'climate_live' => [
            installCenterConfigField('climate_enable', 'Klimaanlage aktiv', 'select', $bool),
            installCenterConfigField('climate_name', 'Name', 'text', [], '', false, 'Klimaanlage'),
            installCenterConfigField('climate_meter_ip', 'Shelly-Zähler IP', 'text', [], '', false, '192.0.2.102'),
            installCenterConfigField('climate_meter_type', 'Zählertyp', 'select', [
                ['value' => 'shelly_pro3em', 'label' => 'Shelly Pro3EM / 3EM Gen2'],
                ['value' => 'shelly_em_gen1', 'label' => 'Shelly EM Gen1 (2 Kanäle)'],
                ['value' => 'shelly_em_mini_gen4', 'label' => 'Shelly EM Mini Gen4'],
                ['value' => 'shelly_pm_mini', 'label' => 'Shelly PM Mini'],
                ['value' => 'auto', 'label' => 'Auto-Erkennung'],
            ]),
            installCenterConfigField('climate_meter_phase', 'Phase / Kanal', 'select', [
                ['value' => 'a', 'label' => 'A / L1'],
                ['value' => 'b', 'label' => 'B / L2'],
                ['value' => 'c', 'label' => 'C / L3'],
                ['value' => 'channel0', 'label' => 'Kanal 0 (Shelly EM Gen1)'],
                ['value' => 'channel1', 'label' => 'Kanal 1 (Shelly EM Gen1)'],
                ['value' => 'total', 'label' => 'Summe'],
            ]),
            installCenterConfigField('climate_min_power_w', 'Aktiv ab W', 'number', [], '', false, '50'),
            installCenterConfigField('climate_poll_s', 'Leseintervall (s)', 'number', [], '', false, '15'),
            installCenterConfigField('climate_history_enable', 'History speichern', 'select', $bool),
            installCenterConfigField('climate_history_interval_s', 'Erfassungsintervall (s)', 'number', [], '', false, '60'),
            installCenterConfigField('climate_forecast_enable', 'Klima-Prognose', 'select', $bool, 'Aus gemessener Klima-Historie und Wetter-/Außentemperatur; keine Schaltbefehle.'),
        ],
        'climate_control' => [
            installCenterConfigField('climate_control_enable', 'Klima-Status aktiv', 'select', $bool, 'Liest Toshiba read-only und schreibt den Status; keine aktiven Toshiba-Kommandos.'),
            installCenterConfigField('climate_control_provider', 'Provider', 'select', [
                ['value' => 'toshiba_cloud', 'label' => 'Toshiba Cloud read-only'],
                ['value' => 'local_only', 'label' => 'Lokal vorbereitet'],
            ]),
            installCenterConfigField('climate_control_mode', 'Modus', 'select', [
                ['value' => 'off', 'label' => 'Aus'],
                ['value' => 'manual', 'label' => 'Manuell'],
                ['value' => 'schedule', 'label' => 'Zeitprofil'],
            ]),
            installCenterConfigField('climate_toshiba_cloud_enable', 'Toshiba Cloud lesen', 'select', $bool, 'Read-only-Lesepfad für Temperaturen, Sollwert und Modus. Steuerbefehle bleiben gesperrt.'),
            installCenterConfigField('climate_toshiba_username', 'Toshiba Benutzer', 'text'),
            installCenterConfigField('climate_toshiba_password', 'Toshiba Passwort', 'password'),
            installCenterConfigField('climate_toshiba_device_ids', 'Toshiba Geräteauswahl', 'text', [], '', false, 'Oben, Unten'),
            installCenterConfigField('climate_day_temp_c', 'Tag-Temperatur °C', 'number', [], '', false, '24.0'),
            installCenterConfigField('climate_night_temp_c', 'Nacht-Temperatur °C', 'number', [], '', false, '26.0'),
            installCenterConfigField('climate_night_start', 'Nacht ab', 'text', [], '', false, '22:00'),
            installCenterConfigField('climate_night_end', 'Nacht bis', 'text', [], '', false, '06:00'),
            installCenterConfigField('climate_night_eco_enable', 'Nacht Eco', 'select', $bool),
            installCenterConfigField('climate_night_quiet_enable', 'Nacht Leise', 'select', $bool),
            installCenterConfigField('climate_high_power_enable', 'High Power tagsüber', 'select', $bool),
        ],
        'ha' => [
            installCenterConfigField('ha_mode', 'Cluster-Rolle', 'select', $haModes),
            installCenterConfigField('ha_peer_ip', 'Partner-IP'),
            installCenterConfigField('ha_sync_interval', 'Sync-Intervall (Min)', 'number'),
            installCenterConfigField('ha_fail_timeout', 'Failover Timeout (Min)', 'number'),
            installCenterConfigField('ha_auto_recover', 'Auto-Recover', 'select', $bool),
            installCenterConfigField('ha_auto_failover', 'Auto-Failover', 'select', $bool),
        ],
        'shadow' => [
            installCenterConfigField('ha_mode', 'Cluster-Rolle', 'select', $haModes),
            installCenterConfigField('shadow_master_url', 'Shadow Master URL', 'text', [], '', false, 'http://192.168.1.10'),
            installCenterConfigField('shadow_snapshot_token', 'Shadow Snapshot Token', 'password', [], 'Exakt 64 Hex-Zeichen; auf Master und Shadow identisch. Leer lassen = unverändert.', true, '64 Hex-Zeichen'),
            installCenterConfigField('shadow_master_ip', 'Shadow Master IP', 'text', [], 'Fallback, wenn keine URL gesetzt ist.', false, '192.168.1.10'),
            installCenterConfigField('ha_peer_ip', 'Partner-IP Fallback', 'text', [], 'Wird genutzt, wenn keine Shadow Master URL/IP gesetzt ist.', false, '192.168.1.11'),
            installCenterConfigField('shadow_sync_interval_s', 'Shadow Takt (s)', 'number', [], '', false, '5'),
            installCenterConfigField('shadow_fetch_timeout_s', 'HTTP Timeout (s)', 'number', [], '', false, '2.5'),
            installCenterConfigField('shadow_snapshot_max_age_s', 'Max. Snapshot-Alter (s)', 'number', [], '', false, '30'),
        ],
        'matter' => [
            installCenterConfigField('matter_bridge', 'Matter Bridge aktivieren', 'select', $bool),
        ],
        'bluelink' => [
            installCenterConfigField('bluelink_refresh_token', 'Refresh Token', 'password', [], 'Leer lassen = unverändert.', true),
            installCenterConfigField('bluelink_vin', 'VIN'),
            installCenterConfigField('bluelink_car_name', 'Fahrzeugname'),
            installCenterConfigField('bluelink_interval', 'Intervall (Min)', 'number', [], '', false, '15'),
            installCenterConfigField('bluelink_ignore_plug_status', 'Plug-Status ignorieren', 'select', $bool),
        ],
        'notifier' => [
            installCenterConfigField('telegram_token', 'Telegram Bot Token', 'password', [], 'Leer lassen = unverändert.', true),
            installCenterConfigField('telegram_chat_id', 'Telegram Chat-ID'),
            installCenterConfigField('telegram_device_name', 'Gerätename'),
            installCenterConfigField('telegram_status_enable', 'Täglicher Statusbericht', 'select', $bool),
            installCenterConfigField('telegram_status_time', 'Status-Uhrzeit', 'time'),
            installCenterConfigField('telegram_stats_enable', 'Tägliche Statistik', 'select', $bool),
            installCenterConfigField('telegram_stats_time', 'Statistik-Uhrzeit', 'time'),
            installCenterConfigField('telegram_weekly_enable', 'Wöchentliche Statistik', 'select', $bool),
            installCenterConfigField('telegram_weekly_time', 'Wochenbericht-Uhrzeit', 'time'),
            installCenterConfigField('telegram_weekly_day', 'Wochenbericht-Tag', 'select', [
                ['value' => '0', 'label' => 'Montag'],
                ['value' => '1', 'label' => 'Dienstag'],
                ['value' => '2', 'label' => 'Mittwoch'],
                ['value' => '3', 'label' => 'Donnerstag'],
                ['value' => '4', 'label' => 'Freitag'],
                ['value' => '5', 'label' => 'Samstag'],
                ['value' => '6', 'label' => 'Sonntag']
            ]),
        ],
        'mqtt' => [
            installCenterConfigField('mqtt_hub_ip', 'MQTT Broker IP', 'text', [], '', false, '127.0.0.1'),
            installCenterConfigField('mqtt_hub_port', 'MQTT Port', 'number', [], '', false, '1883'),
            installCenterConfigField('mqtt_hub_topic', 'Basis-Topic', 'text', [], '', false, 'e3dc'),
            installCenterConfigField('mqtt_hub_user', 'MQTT Benutzer'),
            installCenterConfigField('mqtt_hub_pass', 'MQTT Passwort', 'password', [], 'Leer lassen = unverändert.', true),
            installCenterConfigField('mqtt_hub_sub_soc_topic', 'SoC-Topic Fahrzeug 1'),
            installCenterConfigField('mqtt_hub_sub_soc_name', 'Name Fahrzeug 1'),
            installCenterConfigField('mqtt_hub_sub_soc_topic_2', 'SoC-Topic Fahrzeug 2'),
            installCenterConfigField('mqtt_hub_sub_soc_name_2', 'Name Fahrzeug 2'),
            installCenterConfigField('mqtt_ha_inbound_enable', 'HA/ioBroker Messwerte annehmen', 'select', $bool),
            installCenterConfigField('mqtt_ha_inbound_history_enable', 'Messwerte für History/Prognose nutzen', 'select', $bool),
        ],
    ];
    return $fields[$moduleKey] ?? [];
}

function installCenterConfigScalar($config, $key, $default = '') {
    $key = strtolower((string)$key);
    if (!is_array($config) || !array_key_exists($key, $config) || $config[$key] === null) {
        return $default;
    }
    if (is_bool($config[$key])) {
        return $config[$key] ? '1' : '0';
    }
    return trim((string)$config[$key]);
}

function installCenterConfigHasAddress($config, $key) {
    $value = installCenterConfigScalar($config, $key, '');
    if ($value === '' || $value === '0' || $value === '0.0.0.0') {
        return false;
    }
    return strtolower($value) !== 'null';
}

function installCenterNativeWpLockType($config) {
    $wpType = installCenterConfigScalar($config, 'wp_type', '');
    $hasIdm = installCenterConfigHasAddress($config, 'idm_ip');
    $hasLux = installCenterConfigHasAddress($config, 'luxtronik_ip');
    $hasStiebel = installCenterConfigHasAddress($config, 'stiebel_isg_ip');
    $hasDimplex = installCenterConfigHasAddress($config, 'dimplex_ip');
    if ($wpType === '5' && $hasDimplex) return '5';
    if ($wpType === '4' && $hasStiebel) return '4';
    if ($wpType === '1' && $hasIdm) return '1';
    if ($wpType === '0' && $hasLux) return '0';
    if ($hasDimplex) return '5';
    if ($hasStiebel) return '4';
    if ($hasIdm) return '1';
    if ($hasLux) return '0';
    return null;
}

function installCenterGuardWpTypeUpdates($config, $updates) {
    $warnings = [];
    if (!array_key_exists('wp_type', $updates)) {
        return [$updates, $warnings];
    }
    $current = installCenterConfigScalar($config, 'wp_type', '-1');
    $requested = (string)$updates['wp_type'];
    $nativeLock = installCenterNativeWpLockType($config);
    $safeCurrent = in_array($current, ['-1', '0', '1', '3', '4', '5'], true) ? $current : ($nativeLock ?? '-1');

    if ($requested === '-1') {
        return [$updates, $warnings];
    }

    if ($requested === '2') {
        $updates['wp_type'] = '2';
        $updates['heizstab'] = '1';
        $warnings = [];
        return [$updates, $warnings];
    }
    if ($nativeLock !== null && $requested !== $nativeLock) {
        $updates['wp_type'] = $nativeLock;
        $warnings[] = 'Vorhandene native Wärmepumpen-Konfiguration geschützt. Der Wärmepumpen-Typ wurde nicht über das Modul-Popup umgeschaltet.';
    }
    return [$updates, $warnings];
}

function installCenterDecorateConfigField($field, $config) {
    $shadowDefaults = [
        'shadow_sync_interval_s' => '5',
        'shadow_fetch_timeout_s' => '2.5',
        'shadow_snapshot_max_age_s' => '30',
    ];
    $key = strtolower((string)($field['key'] ?? ''));
    if (in_array($key, ['wb1_e3dc_wbchar6_compat_enable', 'wb2_e3dc_wbchar6_compat_enable'], true)
        && !array_key_exists($key, $config)) {
        $field['value'] = '1';
        $field['help'] = trim(($field['help'] ?? '') . ' Bei einem noch nicht gespeicherten Schlüssel ist der empfohlene Community-Pfad vorausgewählt; eine bewusst gespeicherte 0 wird nie überschrieben.');
    }
    if (array_key_exists($key, $shadowDefaults) && trim((string)($field['value'] ?? '')) === '') {
        $field['value'] = $shadowDefaults[$key];
        $field['help'] = trim(($field['help'] ?? '') . ' Leer wird als Default ' . $shadowDefaults[$key] . ' gespeichert.');
    }
    if (($field['key'] ?? '') !== 'wp_type') {
        return $field;
    }
    $nativeLock = installCenterNativeWpLockType($config);
    if ($nativeLock === null) {
        return $field;
    }
    $field['value'] = $nativeLock;
    $field['help'] = trim(($field['help'] ?? '') . ' Native WP-Konfiguration erkannt; Umschalten ist hier gesperrt, damit IDM/Luxtronik-Werte nicht versehentlich ersetzt werden.');
    foreach ($field['options'] as &$option) {
        $option['disabled'] = ((string)($option['value'] ?? '') !== $nativeLock);
    }
    unset($option);
    return $field;
}

function installCenterBuildModuleConfigPayload($moduleKey) {
    if (!preg_match('/^[a-z0-9_]+$/', (string)$moduleKey)) {
        return ['success' => false, 'error' => 'Ungültiger Modul-Key'];
    }
    $fields = installCenterModuleConfigFields($moduleKey);
    if (!$fields) {
        return ['success' => false, 'error' => 'Für dieses Modul sind keine Popup-Config-Felder freigegeben'];
    }
    $loaded = loadE3dcConfig();
    if (!empty($loaded['error'])) {
        return ['success' => false, 'error' => 'Konfiguration konnte nicht geladen werden'];
    }
    $config = $loaded['config'] ?? [];
    foreach ($fields as &$field) {
        $key = strtolower($field['key']);
        $legacyWb2Missing = (
            $key === 'wb_native_type2'
            && !array_key_exists('wb_native_type2', $config)
        );
        $legacyWb2Blank = (
            $key === 'wb_native_type2'
            && array_key_exists('wb_native_type2', $config)
            && trim((string)$config['wb_native_type2']) === ''
        );
        $hasValue = array_key_exists($key, $config) && (string)$config[$key] !== '';
        $field['has_value'] = $hasValue;
        $field['value'] = !empty($field['secret']) ? '' : (string)($config[$key] ?? '');
        $field = installCenterDecorateConfigField($field, $config);
        if ($legacyWb2Missing) {
            array_unshift($field['options'], [
                'value' => '__legacy_missing__',
                'label' => 'Altbestand automatisch erkennen (unverändert)',
            ]);
            $field['value'] = '__legacy_missing__';
            $field['has_value'] = false;
        } elseif ($legacyWb2Blank) {
            array_unshift($field['options'], [
                'value' => '__legacy_blank__',
                'label' => 'Leerer Altbestandswert – automatisch erkennen (unverändert)',
            ]);
            $field['value'] = '__legacy_blank__';
            $field['has_value'] = false;
        }
    }
    unset($field);
    return [
        'success' => true,
        'module' => $moduleKey,
        'fields' => $fields,
        'config_url' => 'index.php?seite=config'
    ];
}

function installCenterNormalizeConfigValue($field, $value) {
    $type = $field['type'] ?? 'text';
    if ($type === 'select') {
        $allowed = array_map(fn($opt) => (string)($opt['value'] ?? ''), $field['options'] ?? []);
        $value = (string)$value;
        return in_array($value, $allowed, true) ? $value : ($allowed[0] ?? '');
    }
    $value = is_string($value) ? trim($value) : $value;
    if ($type === 'number' && $value !== '' && is_numeric($value)) {
        return strpos((string)$value, '.') !== false ? (float)$value : (int)$value;
    }
    return $value;
}

function installCenterPreserveMissingWb2Type($postedValues, $existingConfig) {
    if (!is_array($postedValues)) {
        return [];
    }
    if (!array_key_exists('wb_native_type2', $postedValues)) {
        return $postedValues;
    }
    $existingConfig = is_array($existingConfig) ? $existingConfig : [];
    $postedType = trim((string)$postedValues['wb_native_type2']);
    $existingMissing = !array_key_exists('wb_native_type2', $existingConfig);
    $existingBlank = !$existingMissing
        && trim((string)$existingConfig['wb_native_type2']) === '';
    if ($postedType === '__legacy_missing__') {
        unset($postedValues['wb_native_type2']);
    } elseif ($postedType === '__legacy_blank__') {
        if ($existingBlank) {
            $postedValues['wb_native_type2'] = '';
        } else {
            unset($postedValues['wb_native_type2']);
        }
    } elseif ($postedType === '' && ($existingMissing || $existingBlank)) {
        if ($existingMissing) {
            unset($postedValues['wb_native_type2']);
        } else {
            $postedValues['wb_native_type2'] = '';
        }
    }
    return $postedValues;
}

function installCenterBackupConfig(&$error = null, $options = []) {
    $options = is_array($options) ? $options : [];
    $configFile = (string)($options['v4_path'] ?? '/var/www/html/data/e3dc_v4.json');
    $backupDir = (string)($options['backup_dir'] ?? '/var/www/html/data/config_backups');
    $error = null;
    $options['backup_dir'] = $backupDir;
    $result = e3dcCreateConfirmedV4Backup($configFile, 'install_center', $options);
    if (empty($result['success'])) {
        $error = 'Config-Backup nicht bestätigt (' . (string)($result['status'] ?? 'unknown') . ')';
        return null;
    }
    return (string)$result['path'];
}

function installCenterSaveModuleConfig($moduleKey, $postedValues) {
    if (!preg_match('/^[a-z0-9_]+$/', (string)$moduleKey)) {
        return ['success' => false, 'error' => 'Ungültiger Modul-Key'];
    }
    if (!is_array($postedValues)) {
        return ['success' => false, 'error' => 'Keine Werte übergeben'];
    }
    $fields = installCenterModuleConfigFields($moduleKey);
    if (!$fields) {
        return ['success' => false, 'error' => 'Für dieses Modul sind keine Popup-Config-Felder freigegeben'];
    }
    $loaded = loadE3dcConfig();
    if (!empty($loaded['error'])) {
        return ['success' => false, 'error' => 'Konfiguration konnte nicht geladen werden'];
    }
    $config = $loaded['config'] ?? [];
    $postedValues = installCenterPreserveMissingWb2Type($postedValues, $config);
    $updates = [];
    $shadowDefaults = [
        'shadow_sync_interval_s' => '5',
        'shadow_fetch_timeout_s' => '2.5',
        'shadow_snapshot_max_age_s' => '30',
    ];
    foreach ($fields as $field) {
        $key = strtolower($field['key']);
        if (!array_key_exists($key, $postedValues)) {
            continue;
        }
        $raw = $postedValues[$key];
        if (!empty($field['secret']) && trim((string)$raw) === '') {
            continue;
        }
        if (
            $key === 'shadow_snapshot_token'
            && preg_match('/\A[0-9a-fA-F]{64}\z/D', trim((string)$raw)) !== 1
        ) {
            return [
                'success' => false,
                'error' => 'Shadow Snapshot Token muss aus exakt 64 Hex-Zeichen bestehen.',
            ];
        }
        if (array_key_exists($key, $shadowDefaults) && trim((string)$raw) === '') {
            $raw = $shadowDefaults[$key];
        }
        if ($key === 'shadow_snapshot_token') {
            $raw = strtolower(trim((string)$raw));
        }
        $updates[$key] = installCenterNormalizeConfigValue($field, $raw);
    }
    [$updates, $guardWarnings] = installCenterGuardWpTypeUpdates($config, $updates);
    if (!$updates) {
        return ['success' => true, 'message' => 'Keine Änderung gespeichert.'];
    }
    $saveResult = saveE3dcConfigValuesDetailed($updates);
    if (empty($saveResult['success'])) {
        $status = (string)($saveResult['status'] ?? 'unknown');
        $error = !empty($saveResult['state_unknown'])
            ? 'Konfiguration wurde veröffentlicht, Cache-/Rückfallzustand ist unklar'
            : (!empty($saveResult['rolled_back'])
                ? 'Konfigurationsänderung wurde nach Cachefehler zurückgesetzt'
                : 'Konfiguration wurde vor dem Commit nicht geändert');
        return [
            'success' => false,
            'error' => $error . ' (' . $status . ')',
            'backup' => $saveResult['backup_path'] ?? null,
        ];
    }
    $backupPath = (string)($saveResult['backup_path'] ?? '');
    $message = count($updates) . ' Wert(e) gespeichert. Bitte betroffene Dienste bei Bedarf neu starten.';
    if (!empty($guardWarnings)) {
        $message .= ' Schutz: ' . implode(' ', $guardWarnings);
    }
    return [
        'success' => true,
        'message' => $message,
        'updated_keys' => array_keys($updates),
        'warnings' => $guardWarnings,
        'backup' => $backupPath
    ];
}

function installCenterRedactText($text) {
    $text = (string)$text;
    $text = preg_replace('/[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}/i', '[redacted-email]', $text);
    $locationKeys = 'lat|lon|lng|long|latitude|longitude|breitengrad|laenge|länge|laengengrad|längengrad|height|hoehe|höhe|elevation|altitude';
    $text = preg_replace('/([?&](?:' . $locationKeys . ')=)[^&\s"\']+/iu', '$1[redacted]', $text);
    $text = preg_replace('/("?(?:' . $locationKeys . ')"?\s*[:=]\s*)-?\d+(?:[.,]\d+)?/iu', '$1[redacted]', $text);
    $secretKeys = 'password|passwort|pwd|pass|pw|token|secret|api[_-]?key|apikey|aes_password|chat[_-]?id|credential|auth';
    $text = preg_replace('/((?:' . $secretKeys . ')\s*[:=]\s*)[^,\s"\'\]\}\)]+/iu', '$1[redacted]', $text);
    $text = preg_replace_callback(
        '/\b(?:\d{1,3}\.){3}\d{1,3}\b/',
        function($match) {
            $ip = (string)($match[0] ?? '');
            return filter_var($ip, FILTER_VALIDATE_IP, FILTER_FLAG_IPV4)
                ? installCenterDiagnosticPseudonym('ip', $ip)
                : $ip;
        },
        $text
    );
    $text = preg_replace_callback(
        '/((?:car|vehicle)[_-]?(?:name|id)|vin|mqtt[_-]?topic|topic)\s*[:=]\s*(?:"([^"]*)"|\'([^\']*)\'|([^,;\r\n\]\}\)]+))/iu',
        function($match) {
            $value = (string)($match[2] ?? '');
            if ($value === '') $value = (string)($match[3] ?? '');
            if ($value === '') $value = trim((string)($match[4] ?? ''));
            return (string)$match[1] . '=' . installCenterDiagnosticPseudonym((string)$match[1], $value);
        },
        $text
    );
    return $text;
}

function installCenterDiagnosticPseudonym($key, $value) {
    $key = strtolower(trim((string)$key));
    $value = trim((string)$value);
    $prefix = preg_match('/(?:car|vehicle|vin)/i', $key) ? 'vehicle' : (str_contains($key, 'topic') ? 'topic' : 'ip');
    return '[' . $prefix . '-' . substr(hash('sha256', $key . "\0" . $value), 0, 10) . ']';
}

function installCenterIsPseudonymousDiagnosticKey($key) {
    $key = strtolower(trim((string)$key));
    if ($key === '') return false;
    if (preg_match('/(^|[_-])(car|vehicle)([_-]|$).*(name|id)|^(car_name|car_id|vehicle_name|vehicle_id|vin)$/i', $key)) {
        return true;
    }
    return preg_match('/(^|[_-])(ip|host|hostname|topic)([_-]|$)|(_ip|_host|_hostname|_topic)$/i', $key) === 1;
}

function installCenterIsSensitiveConfigKey($key) {
    $key = (string)$key;
    if (preg_match('/(password|passwort|pwd|(?:^|[_-])pass(?:$|[_-])|(?:^|[_-])pw(?:$|[_-])|token|secret|api.?key|apikey|credential|auth)/iu', $key) === 1) {
        return true;
    }
    return preg_match('/(password|passwort|pwd|token|secret|api.?key|apikey|mail|email|chat.?id|latitude|longitude|breitengrad|laenge|länge|laengengrad|längengrad|^lat$|^lon$|^lng$|^long$|height|hoehe|höhe|elevation|altitude)/iu', (string)$key) === 1;
}

function installCenterRedactConfigValue($key, $value) {
    if (installCenterIsSensitiveConfigKey($key)) {
        return '[redacted]';
    }
    if (installCenterIsPseudonymousDiagnosticKey($key) && is_scalar($value) && trim((string)$value) !== '') {
        return installCenterDiagnosticPseudonym($key, $value);
    }
    if (is_array($value)) {
        $out = [];
        foreach ($value as $childKey => $childValue) {
            $out[$childKey] = installCenterRedactConfigValue((string)$childKey, $childValue);
        }
        return $out;
    }
    if (is_string($value)) {
        return installCenterRedactText($value);
    }
    return $value;
}

function installCenterRedactedConfig() {
    $loaded = loadE3dcConfig();
    if (!empty($loaded['error'])) {
        return ['error' => 'Konfiguration konnte nicht geladen werden'];
    }
    $config = $loaded['config'] ?? [];
    $redacted = [];
    foreach ($config as $key => $value) {
        $redacted[$key] = installCenterRedactConfigValue((string)$key, $value);
    }
    return [
        '_privacy_note' => 'Automatisch bereinigte Config: Passwörter, Tokens, E-Mail-Adressen und Standortwerte wurden maskiert.',
        'config' => $redacted
    ];
}

function installCenterFileMeta($path) {
    $exists = is_file($path);
    return [
        'path' => $path,
        'exists' => $exists,
        'readable' => $exists ? is_readable($path) : false,
        'size_bytes' => $exists ? (@filesize($path) ?: 0) : null,
        'modified_at' => $exists ? date('c', @filemtime($path) ?: time()) : null,
    ];
}

function installCenterDirMeta($path) {
    $exists = is_dir($path);
    return [
        'path' => $path,
        'exists' => $exists,
        'readable' => $exists ? is_readable($path) : false,
        'writable' => $exists ? is_writable($path) : false,
        'modified_at' => $exists ? date('c', @filemtime($path) ?: time()) : null,
    ];
}

function installCenterSqliteCount($dbPath, $table) {
    if (!class_exists('SQLite3') || !is_file($dbPath)) {
        return null;
    }
    try {
        $db = new SQLite3($dbPath, SQLITE3_OPEN_READONLY);
        $safeTable = preg_replace('/[^a-zA-Z0-9_]/', '', (string)$table);
        $res = $db->querySingle("SELECT COUNT(*) FROM " . $safeTable);
        $db->close();
        return is_numeric($res) ? (int)$res : null;
    } catch (Throwable $e) {
        return null;
    }
}

function installCenterMlDockerStatus() {
    $predictionPath = '/var/www/html/ramdisk/ml_prediction.json';
    $cachePredictionPath = '/var/www/html/data/docker_ramdisk_cache/ml_prediction.json';
    $dbPath = '/var/www/html/data/e3dc_stats.db';
    $predictionExists = is_file($predictionPath);
    $cachePredictionExists = is_file($cachePredictionPath);
    $trainingRows = installCenterSqliteCount($dbPath, 'ml_training_data');
    $dailyRows = installCenterSqliteCount($dbPath, 'daily_stats');

    $status = 'prediction_available';
$message = 'ML-Prognosedatei vorhanden. Frische und Modellintegrität werden ausschließlich im Predictor über Manifest und Hash geprüft.';
    if (!$predictionExists && $cachePredictionExists) {
        $status = 'prediction_not_restored';
$message = 'Aktuelle Ramdisk-Prognose fehlt, ein Warmstart-Cache ist vorhanden. Der Predictor muss ihn sicher wiederherstellen und das private Modell prüfen.';
    } elseif (!$predictionExists) {
        $status = 'no_prediction';
        $message = 'Keine aktuelle ML-Prognose vorhanden. Der private Modellstatus wird aus dem Webprozess bewusst nicht abgefragt.';
    }
    if (!$predictionExists && $trainingRows !== null && $trainingRows < 50) {
        $status = 'not_enough_training_data';
        $message = 'Weniger als 50 ML-Trainingsdatensätze vorhanden. Bis zum sicheren Training bleibt der konservative Ersatzpfad aktiv.';
    }

    return [
        'created_at' => date('c'),
        'docker_detected' => is_file('/.dockerenv') || getenv('E3DC_WEB_PORT') !== false || getenv('E3DC_WEB_BIND') !== false,
        'status' => $status,
        'message' => $message,
        'data_dir' => installCenterDirMeta('/var/www/html/data'),
        'ramdisk_dir' => installCenterDirMeta('/var/www/html/ramdisk'),
        'docker_ramdisk_cache_dir' => installCenterDirMeta('/var/www/html/data/docker_ramdisk_cache'),
        'private_model' => [
            'storage' => 'private_system_store',
            'web_readable' => false,
            'verification' => 'predictor_manifest_and_hash',
        ],
        'prediction' => installCenterFileMeta($predictionPath),
        'warmstart_prediction_cache' => installCenterFileMeta($cachePredictionPath),
        'database' => installCenterFileMeta($dbPath),
        'ml_training_data_rows' => $trainingRows,
        'daily_stats_rows' => $dailyRows,
        'note' => 'ml_prediction.json ist Ramdisk/Warmstart. Das persistente Modell liegt ausserhalb des Webzugriffs und wird hier weder gelesen noch offengelegt.',
    ];
}

function installCenterReadJsonAssoc($path) {
    if (!is_readable($path)) return [];
    $raw = @file_get_contents($path);
    if (!is_string($raw) || trim($raw) === '') return [];
    $decoded = json_decode($raw, true);
    return is_array($decoded) ? $decoded : [];
}

function installCenterPowerRound($value) {
    if (!is_numeric($value)) return null;
    return round((float)$value, 3);
}

function installCenterPowerDecisionSignal($live, $stabilitySignals, $field) {
    $signal = (isset($stabilitySignals[$field]) && is_array($stabilitySignals[$field])) ? $stabilitySignals[$field] : [];
    $raw = installCenterPowerRound($signal['raw_w'] ?? ($live[$field] ?? null));
    $ewma = installCenterPowerRound($signal['ewma_w'] ?? ($live[$field . '_EWMA'] ?? null));
    $decision = installCenterPowerRound($signal['decision_w'] ?? ($live[$field . '_Decision'] ?? null));
    $valid = $signal['valid'] ?? ($live[$field . '_Decision_Valid'] ?? null);
    if ($raw === null && $ewma === null && $decision === null && $valid === null) return null;
    $out = [];
    if ($raw !== null) $out['raw_w'] = $raw;
    if ($ewma !== null) $out['ewma_w'] = $ewma;
    if ($decision !== null) $out['decision_w'] = $decision;
    if ($valid !== null) $out['valid'] = (bool)$valid;
    foreach (['held_by_deadband', 'held_previous_invalid', 'reset'] as $key) {
        if (array_key_exists($key, $signal)) $out[$key] = (bool)$signal[$key];
    }
    return $out;
}

function installCenterParseGlitchTs($row) {
    foreach ([$row, (isset($row['current']) && is_array($row['current'])) ? $row['current'] : []] as $source) {
        if (!is_array($source)) continue;
        foreach (['ts', '_ts', 'timestamp'] as $key) {
            if (!isset($source[$key]) || !is_numeric($source[$key])) continue;
            $ts = (float)$source[$key];
            if ($ts > 10000000000) $ts = $ts / 1000.0;
            if ($ts > 0) return $ts;
        }
    }
    return 0.0;
}

function installCenterGlitchPathMayContainRecent($path, $cutoff) {
    if ((float)$cutoff <= 0) return true;
    $base = basename((string)$path);
    if (!preg_match('/^live_plausibility_glitches_(\d{8})\.jsonl(?:\.gz)?$/', $base, $m)) {
        return true;
    }
    $timezone = new DateTimeZone(date_default_timezone_get());
    $dayStart = DateTimeImmutable::createFromFormat('!Ymd', (string)$m[1], $timezone);
    if (!$dayStart) return true;
    $dayEnd = $dayStart->modify('+1 day')->getTimestamp();
    return $dayEnd > (float)$cutoff;
}

function installCenterGlitchReasons($row) {
    $current = (isset($row['current']) && is_array($row['current'])) ? $row['current'] : [];
    $aggregation = (isset($row['aggregation']) && is_array($row['aggregation'])) ? $row['aggregation'] : [];
    $signature = (isset($aggregation['signature']) && is_array($aggregation['signature'])) ? $aggregation['signature'] : [];
    $reasons = array_key_exists('reasons', $signature)
        ? $signature['reasons']
        : ($current['reasons'] ?? ($row['reasons'] ?? []));
    if (is_string($reasons)) return [$reasons];
    if (!is_array($reasons)) return [];
    return array_values(array_map('strval', $reasons));
}

function installCenterGlitchEventKind($row) {
    $aggregation = (isset($row['aggregation']) && is_array($row['aggregation'])) ? $row['aggregation'] : [];
    $kind = trim((string)($aggregation['event_kind'] ?? 'legacy_sample'));
    return $kind !== '' ? $kind : 'legacy_sample';
}

function installCenterGlitchSampleCount($row) {
    $aggregation = (isset($row['aggregation']) && is_array($row['aggregation'])) ? $row['aggregation'] : [];
    if (!array_key_exists('sample_count', $aggregation)) return 1;
    return max(0, (int)$aggregation['sample_count']);
}

function installCenterReadGlitchRows($maxAgeHours = 48) {
    static $requestCache = [];
    $cacheKey = (string)max(1, (int)$maxAgeHours);
    if (array_key_exists($cacheKey, $requestCache)) {
        return $requestCache[$cacheKey];
    }
    $cutoff = time() - max(1, (int)$maxAgeHours) * 3600;
    $rows = [];
    $paths = array_merge(
        glob('/var/www/html/logs/live_plausibility_glitches_*.jsonl') ?: [],
        glob('/var/www/html/logs/live_plausibility_glitches_*.jsonl.gz') ?: []
    );
    $paths = array_values(array_filter(
        $paths,
        fn($path) => installCenterGlitchPathMayContainRecent($path, $cutoff)
    ));
    sort($paths, SORT_NATURAL);
    foreach ($paths as $path) {
        $reader = null;
        $isGz = str_ends_with((string)$path, '.gz');
        if ($isGz) {
            if (!function_exists('gzopen')) continue;
            $reader = @gzopen($path, 'rb');
        } else {
            $reader = @fopen($path, 'rb');
        }
        if (!$reader) continue;
        while ($isGz ? !gzeof($reader) : !feof($reader)) {
            $line = $isGz ? @gzgets($reader) : @fgets($reader);
            if (!is_string($line) || trim($line) === '') continue;
            $row = json_decode($line, true);
            if (!is_array($row)) continue;
            $ts = installCenterParseGlitchTs($row);
            if ($ts <= 0 || $ts < $cutoff) continue;
            $rows[] = ['ts' => $ts, 'file' => basename((string)$path), 'row' => $row];
        }
        if ($isGz) @gzclose($reader); else @fclose($reader);
    }
    usort($rows, fn($a, $b) => ($a['ts'] <=> $b['ts']));
    $requestCache[$cacheKey] = $rows;
    return $requestCache[$cacheKey];
}

function installCenterGlitchCompactEvent($item) {
    $row = is_array($item['row'] ?? null) ? $item['row'] : [];
    $current = (isset($row['current']) && is_array($row['current'])) ? $row['current'] : [];
    $previous = (isset($row['previous_valid']) && is_array($row['previous_valid'])) ? $row['previous_valid'] : [];
    $aggregation = (isset($row['aggregation']) && is_array($row['aggregation'])) ? $row['aggregation'] : [];
    $ts = (float)($item['ts'] ?? 0);
    return [
        'time' => $ts > 0 ? date('c', (int)$ts) : null,
        'age_h' => $ts > 0 ? round((time() - $ts) / 3600.0, 3) : null,
        'file' => (string)($item['file'] ?? ''),
        'event_kind' => installCenterGlitchEventKind($row),
        'sample_count' => installCenterGlitchSampleCount($row),
        'window_start' => isset($aggregation['window_start_ts']) && is_numeric($aggregation['window_start_ts'])
            ? date('c', (int)$aggregation['window_start_ts'])
            : null,
        'window_end' => isset($aggregation['window_end_ts']) && is_numeric($aggregation['window_end_ts'])
            ? date('c', (int)$aggregation['window_end_ts'])
            : null,
        'reasons' => installCenterGlitchReasons($row),
        'grid_w' => $current['grid_w'] ?? null,
        'grid_pm_sum_w' => $current['grid_pm_sum_w'] ?? null,
        'grid_pm_delta_w' => $current['grid_pm_delta_w'] ?? null,
        'grid_pm_delta_abs_w' => $current['grid_pm_delta_abs_w'] ?? null,
        'home_w' => $current['home_w'] ?? null,
        'pv_w' => $current['pv_w'] ?? null,
        'battery_w' => $current['battery_w'] ?? null,
        'wallbox_w' => $current['wallbox_w'] ?? null,
        'previous_valid' => $previous['valid'] ?? null,
        'previous_grid_w' => $previous['grid_w'] ?? null,
        'previous_grid_pm_delta_w' => $previous['grid_pm_delta_w'] ?? null,
    ];
}

function installCenterGlitchFloat($value) {
    if ($value === null || $value === '') return null;
    if (!is_numeric($value)) return null;
    return (float)$value;
}

function installCenterGlitchPowerBucket($value, $absValue = true) {
    $num = installCenterGlitchFloat($value);
    if ($num === null) return 'unknown';
    $v = $absValue ? abs($num) : $num;
    if ($v < 250) return '0-250W';
    if ($v < 1000) return '250W-1kW';
    if ($v < 3000) return '1-3kW';
    if ($v < 6000) return '3-6kW';
    if ($v < 12000) return '6-12kW';
    return '>=12kW';
}

function installCenterGlitchGridState($value) {
    $num = installCenterGlitchFloat($value);
    if ($num === null) return 'unknown';
    if ($num > 250) return 'import';
    if ($num < -250) return 'export';
    return 'neutral';
}

function installCenterGlitchBatteryState($value) {
    $num = installCenterGlitchFloat($value);
    if ($num === null) return 'unknown';
    if ($num > 250) return 'charging';
    if ($num < -250) return 'discharging';
    return 'neutral';
}

function installCenterGlitchWallboxState($value) {
    $num = installCenterGlitchFloat($value);
    if ($num === null) return 'unknown';
    if (abs($num) > 6000) return 'active_high';
    if (abs($num) > 500) return 'active_low';
    return 'idle';
}

function installCenterGlitchDeltaSign($value) {
    $num = installCenterGlitchFloat($value);
    if ($num === null) return 'unknown';
    if ($num > 250) return 'grid_gt_pm';
    if ($num < -250) return 'grid_lt_pm';
    return 'within_threshold';
}

function installCenterGlitchReasonKey($row) {
    $reasons = installCenterGlitchReasons(is_array($row) ? $row : []);
    $reasons = array_values(array_filter(array_map('strval', $reasons), fn($item) => trim($item) !== ''));
    sort($reasons, SORT_STRING);
    return $reasons ? implode('+', $reasons) : 'none';
}

function installCenterGlitchTopCounts($counts, $limit = 12) {
    if (!is_array($counts) || !$counts) return [];
    arsort($counts, SORT_NUMERIC);
    return array_slice($counts, 0, max(1, (int)$limit), true);
}

function installCenterGlitchAddCount(&$counts, $key, $weight = 1) {
    $weight = max(0, (int)$weight);
    if ($weight <= 0) return;
    $key = (string)$key;
    if ($key === '') $key = 'unknown';
    $counts[$key] = ($counts[$key] ?? 0) + $weight;
}

function installCenterGlitchSituationEvent($item) {
    $row = is_array($item['row'] ?? null) ? $item['row'] : [];
    $current = (isset($row['current']) && is_array($row['current'])) ? $row['current'] : [];
    $previous = (isset($row['previous_valid']) && is_array($row['previous_valid'])) ? $row['previous_valid'] : [];
    $aggregation = (isset($row['aggregation']) && is_array($row['aggregation'])) ? $row['aggregation'] : [];
    $ts = (float)($item['ts'] ?? 0);
    $eventKind = installCenterGlitchEventKind($row);
    $sampleCount = installCenterGlitchSampleCount($row);
    $maxSampleGapS = isset($aggregation['max_sample_gap_s']) && is_numeric($aggregation['max_sample_gap_s'])
        ? max(0.0, (float)$aggregation['max_sample_gap_s'])
        : null;
    $windowStartTs = isset($aggregation['window_start_ts']) && is_numeric($aggregation['window_start_ts'])
        ? (float)$aggregation['window_start_ts']
        : $ts;
    $windowEndTs = isset($aggregation['window_end_ts']) && is_numeric($aggregation['window_end_ts'])
        ? (float)$aggregation['window_end_ts']
        : $ts;
    if ($windowEndTs < $windowStartTs) $windowEndTs = $windowStartTs;
    $windowContiguous = (
        $sampleCount <= 1
        || ($maxSampleGapS !== null && $maxSampleGapS <= 300.0)
    );
    if (!$windowContiguous) {
        // Historische v2-Sätze ohne Abstandsmetadaten und echte Messlücken
        // dürfen nicht als lückenloser UI-Burst über das ganze Fenster gelten.
        $windowStartTs = $ts;
        $windowEndTs = $ts;
    }
    return [
        'ts' => $ts,
        'time' => $ts > 0 ? date('c', (int)$ts) : null,
        'hour' => $ts > 0 ? date('H', (int)$ts) : null,
        'file' => (string)($item['file'] ?? ''),
        'event_kind' => $eventKind,
        'sample_count' => $sampleCount,
        'max_sample_gap_s' => $maxSampleGapS,
        'window_contiguous' => $windowContiguous,
        'window_start_ts' => $windowStartTs,
        'window_end_ts' => $windowEndTs,
        'is_recovery' => $eventKind === 'recovered',
        'is_close' => in_array($eventKind, ['transition_end', 'shutdown_close'], true),
        'reason_key' => installCenterGlitchReasonKey($row),
        'reasons' => installCenterGlitchReasons($row),
        'errors' => array_slice(is_array($current['errors'] ?? null) ? $current['errors'] : [], 0, 4),
        'source' => $current['source'] ?? null,
        'snapshot_source' => $current['power_snapshot_source'] ?? null,
        'grid_w' => $current['grid_w'] ?? null,
        'grid_state' => installCenterGlitchGridState($current['grid_w'] ?? null),
        'grid_pm_sum_w' => $current['grid_pm_sum_w'] ?? null,
        'grid_pm_delta_w' => $current['grid_pm_delta_w'] ?? null,
        'grid_pm_delta_abs_bucket' => installCenterGlitchPowerBucket($current['grid_pm_delta_abs_w'] ?? $current['grid_pm_delta_w'] ?? null),
        'grid_pm_delta_sign' => installCenterGlitchDeltaSign($current['grid_pm_delta_w'] ?? null),
        'grid_pm_available' => $current['grid_pm_available'] ?? null,
        'grid_pm_source' => $current['grid_pm_source'] ?? null,
        'grid_phase_w' => $current['grid_phase_w'] ?? null,
        'pv_w' => $current['pv_w'] ?? null,
        'pv_bucket' => installCenterGlitchPowerBucket($current['pv_w'] ?? null),
        'battery_w' => $current['battery_w'] ?? null,
        'battery_state' => installCenterGlitchBatteryState($current['battery_w'] ?? null),
        'home_w' => $current['home_w'] ?? null,
        'home_bucket' => installCenterGlitchPowerBucket($current['home_w'] ?? null),
        'home_balance_w' => $current['home_balance_w'] ?? null,
        'home_delta_w' => $current['home_delta_w'] ?? null,
        'home_source' => $current['home_source'] ?? null,
        'wallbox_w' => $current['wallbox_w'] ?? null,
        'wallbox_state' => installCenterGlitchWallboxState($current['wallbox_w'] ?? null),
        'soc' => $current['soc'] ?? null,
        'previous_grid_w' => $previous['grid_w'] ?? null,
        'previous_grid_pm_delta_w' => $previous['grid_pm_delta_w'] ?? null,
        'previous_wallbox_w' => $previous['wallbox_w'] ?? null,
    ];
}

function installCenterGlitchNumericStats($events, $key) {
    $values = [];
    $sampleCount = 0;
    foreach ($events as $event) {
        $weight = max(0, (int)($event['sample_count'] ?? 1));
        if ($weight <= 0) continue;
        $num = installCenterGlitchFloat($event[$key] ?? null);
        if ($num !== null) {
            $values[] = $num;
            $sampleCount += $weight;
        }
    }
    sort($values, SORT_NUMERIC);
    $count = count($values);
    if ($count === 0) {
        return [
            'count' => 0,
            'sample_count' => 0,
            'basis' => 'persisted_window_snapshots_unweighted',
        ];
    }
    return [
        'count' => $count,
        'sample_count' => $sampleCount,
        // Die v2-Datei kennt exakte Samplegewichte, aber keine numerischen
        // Zwischenwerte. Quantile bleiben deshalb ehrlich ungewichtete
        // Fenster-Snapshots und werden nicht als 3-Sekunden-Verteilung ausgegeben.
        'basis' => 'persisted_window_snapshots_unweighted',
        'min' => round($values[0], 1),
        'p50' => round($values[(int)floor(($count - 1) * 0.5)], 1),
        'p90' => round($values[(int)floor(($count - 1) * 0.9)], 1),
        'max' => round($values[$count - 1], 1),
    ];
}

function installCenterGlitchBurstSummary($events) {
    $events = array_values(array_filter(
        $events,
        fn($event) => max(0, (int)($event['sample_count'] ?? 1)) > 0
    ));
    if (!$events) {
        return [
            'gap_s' => 300,
            'method' => 'aggregation_windows',
            'count' => 0,
            'largest' => [],
        ];
    }
    usort($events, function ($a, $b) {
        $startCmp = ((float)($a['window_start_ts'] ?? $a['ts'] ?? 0))
            <=> ((float)($b['window_start_ts'] ?? $b['ts'] ?? 0));
        if ($startCmp !== 0) return $startCmp;
        return ((float)($a['window_end_ts'] ?? $a['ts'] ?? 0))
            <=> ((float)($b['window_end_ts'] ?? $b['ts'] ?? 0));
    });
    $bursts = [];
    $current = [];
    $currentEndTs = 0.0;
    foreach ($events as $event) {
        $startTs = (float)($event['window_start_ts'] ?? $event['ts'] ?? 0);
        $endTs = max($startTs, (float)($event['window_end_ts'] ?? $event['ts'] ?? 0));
        if (!$current || ($startTs - $currentEndTs) <= 300.0) {
            $current[] = $event;
            $currentEndTs = max($currentEndTs, $endTs);
        } else {
            $bursts[] = $current;
            $current = [$event];
            $currentEndTs = $endTs;
        }
    }
    if ($current) $bursts[] = $current;
    $largest = [];
    foreach ($bursts as $burst) {
        $first = $burst[0];
        $reasonCounts = [];
        $wallboxCounts = [];
        $gridCounts = [];
        $pvCounts = [];
        $eventKindCounts = [];
        $sampleCount = 0;
        $snapshotEventCount = 0;
        $startTs = (float)($first['window_start_ts'] ?? $first['ts'] ?? 0);
        $endTs = $startTs;
        foreach ($burst as $event) {
            $weight = max(0, (int)($event['sample_count'] ?? 1));
            $sampleCount += $weight;
            $snapshotWeight = $weight > 0 ? 1 : 0;
            $snapshotEventCount += $snapshotWeight;
            $startTs = min($startTs, (float)($event['window_start_ts'] ?? $event['ts'] ?? 0));
            $endTs = max($endTs, (float)($event['window_end_ts'] ?? $event['ts'] ?? 0));
            installCenterGlitchAddCount($reasonCounts, $event['reason_key'] ?? 'unknown', $weight);
            installCenterGlitchAddCount($wallboxCounts, $event['wallbox_state'] ?? 'unknown', $snapshotWeight);
            installCenterGlitchAddCount($gridCounts, $event['grid_state'] ?? 'unknown', $snapshotWeight);
            installCenterGlitchAddCount($pvCounts, $event['pv_bucket'] ?? 'unknown', $snapshotWeight);
            installCenterGlitchAddCount($eventKindCounts, $event['event_kind'] ?? 'legacy_sample');
        }
        $largest[] = [
            'count' => $sampleCount,
            'observation_count' => $sampleCount,
            'snapshot_event_count' => $snapshotEventCount,
            'persisted_event_count' => count($burst),
            'start' => $startTs > 0 ? date('c', (int)$startTs) : null,
            'end' => $endTs > 0 ? date('c', (int)$endTs) : null,
            'duration_min' => round(max(0.0, $endTs - $startTs) / 60.0, 1),
            'event_kind_counts' => installCenterGlitchTopCounts($eventKindCounts, 8),
            'reason_counts' => installCenterGlitchTopCounts($reasonCounts, 5),
            'wallbox_counts' => installCenterGlitchTopCounts($wallboxCounts, 5),
            'grid_counts' => installCenterGlitchTopCounts($gridCounts, 5),
            'pv_counts' => installCenterGlitchTopCounts($pvCounts, 5),
        ];
    }
    usort($largest, fn($a, $b) => ((int)($b['count'] ?? 0) <=> (int)($a['count'] ?? 0)));
    return [
        'gap_s' => 300,
        'method' => 'aggregation_windows',
        'count' => count($bursts),
        'largest' => array_slice($largest, 0, 8),
    ];
}

function installCenterGlitchSituationWindowSummary($events, $hours) {
    $cutoff = time() - max(1, (int)$hours) * 3600;
    $rows = [];
    foreach ($events as $item) {
        if ((float)($item['ts'] ?? 0) >= $cutoff) $rows[] = installCenterGlitchSituationEvent($item);
    }
    $counts = [
        'reason_counts' => [],
        'by_hour' => [],
        'by_wallbox_state' => [],
        'by_grid_state' => [],
        'by_battery_state' => [],
        'by_pv_bucket' => [],
        'by_home_bucket' => [],
        'by_delta_abs_bucket' => [],
        'by_delta_sign' => [],
        'by_snapshot_source' => [],
        'by_grid_pm_source' => [],
        'top_situations' => [],
        'event_kind_counts' => [],
    ];
    $observationCount = 0;
    $snapshotEventCount = 0;
    $recoveryEventCount = 0;
    $closeEventCount = 0;
    foreach ($rows as $event) {
        $weight = max(0, (int)($event['sample_count'] ?? 1));
        $observationCount += $weight;
        // Gründe gehören zur stabilen Aggregationssignatur und dürfen mit der
        // exakten Samplezahl gewichtet werden. Alle Leistungs-/Situationswerte
        // sind dagegen nur der eine persistierte Snapshot des Fensters.
        $snapshotWeight = $weight > 0 ? 1 : 0;
        $snapshotEventCount += $snapshotWeight;
        if (!empty($event['is_recovery'])) $recoveryEventCount++;
        if (!empty($event['is_close'])) $closeEventCount++;
        installCenterGlitchAddCount($counts['event_kind_counts'], $event['event_kind'] ?? 'legacy_sample');
        installCenterGlitchAddCount($counts['reason_counts'], $event['reason_key'] ?? 'unknown', $weight);
        installCenterGlitchAddCount($counts['by_hour'], $event['hour'] ?? 'unknown', $snapshotWeight);
        installCenterGlitchAddCount($counts['by_wallbox_state'], $event['wallbox_state'] ?? 'unknown', $snapshotWeight);
        installCenterGlitchAddCount($counts['by_grid_state'], $event['grid_state'] ?? 'unknown', $snapshotWeight);
        installCenterGlitchAddCount($counts['by_battery_state'], $event['battery_state'] ?? 'unknown', $snapshotWeight);
        installCenterGlitchAddCount($counts['by_pv_bucket'], $event['pv_bucket'] ?? 'unknown', $snapshotWeight);
        installCenterGlitchAddCount($counts['by_home_bucket'], $event['home_bucket'] ?? 'unknown', $snapshotWeight);
        installCenterGlitchAddCount($counts['by_delta_abs_bucket'], $event['grid_pm_delta_abs_bucket'] ?? 'unknown', $snapshotWeight);
        installCenterGlitchAddCount($counts['by_delta_sign'], $event['grid_pm_delta_sign'] ?? 'unknown', $snapshotWeight);
        installCenterGlitchAddCount($counts['by_snapshot_source'], $event['snapshot_source'] ?? 'unknown', $snapshotWeight);
        installCenterGlitchAddCount($counts['by_grid_pm_source'], $event['grid_pm_source'] ?? 'unknown', $snapshotWeight);
        $combo = implode(' | ', [
            $event['reason_key'] ?? 'unknown',
            $event['wallbox_state'] ?? 'unknown',
            $event['grid_state'] ?? 'unknown',
            $event['pv_bucket'] ?? 'unknown',
            $event['battery_state'] ?? 'unknown',
        ]);
        installCenterGlitchAddCount($counts['top_situations'], $combo, $snapshotWeight);
    }
    return [
        'count' => $observationCount,
        'observation_count' => $observationCount,
        'snapshot_event_count' => $snapshotEventCount,
        'context_count_basis' => 'persisted_snapshots_unweighted',
        'persisted_event_count' => count($rows),
        'recovery_event_count' => $recoveryEventCount,
        'close_event_count' => $closeEventCount,
        'per_hour' => round($observationCount / max(1, (int)$hours), 3),
        'first' => $rows ? ($rows[0]['time'] ?? null) : null,
        'last' => $rows ? ($rows[count($rows) - 1]['time'] ?? null) : null,
        'event_kind_counts' => installCenterGlitchTopCounts($counts['event_kind_counts'], 8),
        'reason_counts' => installCenterGlitchTopCounts($counts['reason_counts']),
        'by_hour' => installCenterGlitchTopCounts($counts['by_hour'], 24),
        'by_wallbox_state' => installCenterGlitchTopCounts($counts['by_wallbox_state']),
        'by_grid_state' => installCenterGlitchTopCounts($counts['by_grid_state']),
        'by_battery_state' => installCenterGlitchTopCounts($counts['by_battery_state']),
        'by_pv_bucket' => installCenterGlitchTopCounts($counts['by_pv_bucket']),
        'by_home_bucket' => installCenterGlitchTopCounts($counts['by_home_bucket']),
        'by_delta_abs_bucket' => installCenterGlitchTopCounts($counts['by_delta_abs_bucket']),
        'by_delta_sign' => installCenterGlitchTopCounts($counts['by_delta_sign']),
        'by_snapshot_source' => installCenterGlitchTopCounts($counts['by_snapshot_source']),
        'by_grid_pm_source' => installCenterGlitchTopCounts($counts['by_grid_pm_source']),
        'top_situations' => installCenterGlitchTopCounts($counts['top_situations'], 16),
        'delta_stats_w' => installCenterGlitchNumericStats($rows, 'grid_pm_delta_w'),
        'grid_stats_w' => installCenterGlitchNumericStats($rows, 'grid_w'),
        'pv_stats_w' => installCenterGlitchNumericStats($rows, 'pv_w'),
        'battery_stats_w' => installCenterGlitchNumericStats($rows, 'battery_w'),
        'wallbox_stats_w' => installCenterGlitchNumericStats($rows, 'wallbox_w'),
        'home_stats_w' => installCenterGlitchNumericStats($rows, 'home_w'),
        'bursts' => installCenterGlitchBurstSummary($rows),
        'recent_events' => array_slice($rows, -10),
    ];
}

function installCenterGlitchWindowSummary($events, $hours) {
    $cutoff = time() - max(1, (int)$hours) * 3600;
    $rows = array_values(array_filter($events, fn($item) => (float)($item['ts'] ?? 0) >= $cutoff));
    $reasons = [];
    $files = [];
    $eventKinds = [];
    $observationCount = 0;
    $recoveryEventCount = 0;
    $closeEventCount = 0;
    foreach ($rows as $item) {
        $row = is_array($item['row'] ?? null) ? $item['row'] : [];
        $weight = installCenterGlitchSampleCount($row);
        $eventKind = installCenterGlitchEventKind($row);
        $observationCount += $weight;
        if ($eventKind === 'recovered') $recoveryEventCount++;
        if (in_array($eventKind, ['transition_end', 'shutdown_close'], true)) $closeEventCount++;
        installCenterGlitchAddCount($eventKinds, $eventKind);
        $files[(string)($item['file'] ?? '')] = true;
        foreach (installCenterGlitchReasons($row) as $reason) {
            $reasons[$reason] = ($reasons[$reason] ?? 0) + $weight;
        }
    }
    $last = $rows ? (float)$rows[count($rows) - 1]['ts'] : 0.0;
    return [
        'count' => $observationCount,
        'observation_count' => $observationCount,
        'persisted_event_count' => count($rows),
        'recovery_event_count' => $recoveryEventCount,
        'close_event_count' => $closeEventCount,
        'event_kind_counts' => installCenterGlitchTopCounts($eventKinds, 8),
        'per_hour' => round($observationCount / max(1, (int)$hours), 3),
        'first' => $rows ? date('c', (int)$rows[0]['ts']) : null,
        'last' => $last > 0 ? date('c', (int)$last) : null,
        'last_age_h' => $last > 0 ? round((time() - $last) / 3600.0, 3) : null,
        'reason_counts' => $reasons,
        'files' => array_values(array_filter(array_keys($files))),
    ];
}

function installCenterPowerDecisionDiagnosticsStatus() {
    $live = installCenterReadJsonAssoc('/var/www/html/ramdisk/live_data_py.json');
    $state = installCenterReadJsonAssoc('/var/www/html/ramdisk/live_decision_stability.json');
    $stability = (isset($live['Power_Decision_Stability']) && is_array($live['Power_Decision_Stability']))
        ? $live['Power_Decision_Stability']
        : [];
    $stateSignals = (isset($state['signals']) && is_array($state['signals'])) ? $state['signals'] : [];
    $liveSignals = (isset($stability['signals']) && is_array($stability['signals'])) ? $stability['signals'] : [];
    $signalsMeta = [];
    foreach ($stateSignals as $field => $signal) {
        if (is_array($signal)) $signalsMeta[$field] = $signal;
    }
    foreach ($liveSignals as $field => $signal) {
        if (!is_array($signal)) continue;
        $signalsMeta[$field] = array_merge($signalsMeta[$field] ?? [], $signal);
    }
    $signals = [];
    foreach (['Grid_Power', 'Home_Power', 'PV_Power', 'Battery_Power', 'Wallbox_Power'] as $field) {
        $signal = installCenterPowerDecisionSignal($live, $signalsMeta, $field);
        if ($signal !== null) $signals[$field] = $signal;
    }
    $events = installCenterReadGlitchRows(48);
    return [
        'schema_version' => 'power_decision_diagnosis_v1',
        'created_at' => date('c'),
        'current' => [
            'status' => $stability['status'] ?? null,
            'diagnostic_only' => $stability['diagnostic_only'] ?? null,
            'hard_stop_bypass' => $stability['hard_stop_bypass'] ?? null,
            'raw_values_preserved' => $stability['raw_values_preserved'] ?? null,
            'sample_valid' => $stability['sample_valid'] ?? ($live['RSCP_Sample_Valid'] ?? null),
            'usable_for_budget' => $stability['usable_for_budget'] ?? ($live['Power_Decision_Usable'] ?? null),
            'glitch_reasons' => $live['RSCP_Glitch_Reasons'] ?? [],
            'signals' => $signals,
            'state_ts' => isset($state['ts']) && is_numeric($state['ts']) ? date('c', (int)$state['ts']) : null,
        ],
        'glitches' => [
            'last_1h' => installCenterGlitchWindowSummary($events, 1),
            'last_6h' => installCenterGlitchWindowSummary($events, 6),
            'last_24h' => installCenterGlitchWindowSummary($events, 24),
            'last_48h' => installCenterGlitchWindowSummary($events, 48),
            'recent_events' => array_map('installCenterGlitchCompactEvent', array_slice($events, -8)),
        ],
        'privacy_note' => 'Enthält nur kompakte Leistungsdiagnose, EWMA-/Decision-Werte und Plausibilitätsgründe; keine Zugangsdaten oder Konfigurationsgeheimnisse.',
    ];
}

function installCenterGlitchSituationDiagnosticsStatus() {
    $events = installCenterReadGlitchRows(48);
    $live = installCenterReadJsonAssoc('/var/www/html/ramdisk/live_data_py.json');
    return [
        'schema_version' => 'glitch_situation_summary_v1',
        'created_at' => date('c'),
        'current_live' => [
            'valid' => $live['RSCP_Sample_Valid'] ?? null,
            'reasons' => $live['RSCP_Glitch_Reasons'] ?? [],
            'pv_w' => $live['PV_Power'] ?? null,
            'grid_w' => $live['Grid_Power'] ?? null,
            'battery_w' => $live['Battery_Power'] ?? null,
            'home_w' => $live['Home_Power'] ?? null,
            'home_power_source' => $live['home_power_source'] ?? ($live['Home_Power_Source'] ?? null),
            'home_power_valid' => $live['home_power_valid'] ?? ($live['Home_Power_Valid'] ?? null),
            'home_power_independent' => $live['home_power_independent'] ?? ($live['Home_Power_Independent'] ?? null),
            'home_balance_w' => $live['home_balance_w'] ?? ($live['Home_Balance_W'] ?? null),
            'home_delta_w' => $live['home_delta_w'] ?? ($live['Home_Delta_W'] ?? null),
            'grid_power_valid' => $live['grid_power_valid'] ?? ($live['Grid_Power_Valid'] ?? null),
            'grid_pm_available' => $live['grid_pm_available'] ?? ($live['Grid_PM_Available'] ?? null),
            'wallbox_w' => $live['Wallbox_Power'] ?? null,
            'grid_pm_delta_w' => $live['Grid_PM_Delta'] ?? null,
            'grid_pm_sum_w' => $live['grid_pm_sum_w'] ?? null,
        ],
        'last_1h' => installCenterGlitchSituationWindowSummary($events, 1),
        'last_6h' => installCenterGlitchSituationWindowSummary($events, 6),
        'last_24h' => installCenterGlitchSituationWindowSummary($events, 24),
        'last_48h' => installCenterGlitchSituationWindowSummary($events, 48),
        'privacy_note' => 'Maschinenlesbare Situationsanalyse aus live_plausibility_glitches: Leistungswerte, Plausibilitätsgründe, Bursts und Kontextklassen; keine Zugangsdaten.',
    ];
}

function installCenterDirectMarketingConfigSubset() {
    $loaded = loadE3dcConfig();
    $config = empty($loaded['error']) && isset($loaded['config']) && is_array($loaded['config']) ? $loaded['config'] : [];
    $keys = [
        'direct_marketing_enable',
        'direct_marketing_mode',
        'direct_marketing_profit_profile',
        'direct_marketing_provider_name',
        'direct_marketing_settlement_basis',
        'direct_marketing_revenue_offset_ct',
        'direct_marketing_fee_ct_per_kwh',
        'direct_marketing_fee_pct',
        'direct_marketing_monthly_fee_eur',
        'direct_marketing_variable_fee_basis',
        'direct_marketing_variable_fee_basis_ct_per_kwh',
        'direct_marketing_service_vat_pct',
        'direct_marketing_input_vat_recoverable',
        'direct_marketing_installed_kwp',
        'direct_marketing_balancing_cost_eur_per_kwp_month',
        'direct_marketing_balancing_cost_actual_eur_per_kwp_month',
        'direct_marketing_min_margin_pct',
        'direct_marketing_min_profit_ct_per_kwh',
        'direct_marketing_profit_hold_ct_per_kwh',
        'direct_marketing_margin_hold_pct',
        'direct_marketing_degradation_ct_per_kwh',
        'direct_marketing_roundtrip_efficiency_pct',
        'direct_marketing_safety_margin_ct_per_kwh',
        'direct_marketing_export_enable',
        'direct_marketing_grid_charge_enable',
        'direct_marketing_pv_store_enable',
        'direct_marketing_pv_store_threshold_ct',
        'direct_marketing_pv_store_max_w',
        'direct_marketing_pv_store_min_surplus_w',
        'direct_marketing_pv_store_import_guard_w',
        'direct_marketing_pv_store_min_hold_s',
        'direct_marketing_pv_store_ramp_step_w',
        'direct_marketing_pv_store_dc_only_enable',
        'direct_marketing_pv_store_external_ac_guard_w',
        'direct_marketing_pv_store_export_limit_guard_w',
        'direct_marketing_pv_store_export_limit_ramp_bypass_w',
        'direct_marketing_v2x_discharge_enable',
        'direct_marketing_max_export_w',
        'direct_marketing_min_grid_export_w',
        'direct_marketing_max_grid_charge_w',
        'direct_marketing_max_cycles_per_day',
        'direct_marketing_max_daily_export_kwh',
        'direct_marketing_min_window_profit_eur',
        'direct_marketing_min_export_energy_kwh',
        'direct_marketing_min_export_window_min',
        'direct_marketing_preferred_export_plateau_min',
        'direct_marketing_price_plateau_tolerance_ct',
        'direct_marketing_deep_cycle_threshold_pct',
        'direct_marketing_deep_cycle_lcos_factor',
        'direct_marketing_home_reserve_soc_pct',
        'direct_marketing_night_reserve_soc_pct',
        'direct_marketing_morning_export_target_soc_pct',
        'direct_marketing_negative_price_no_export',
        'direct_marketing_negative_headroom_enable',
        'direct_marketing_negative_headroom_lookahead_min',
        'direct_marketing_negative_headroom_min_window_min',
        'direct_marketing_negative_headroom_min_surplus_wh',
        'direct_marketing_negative_headroom_buffer_pct',
        'direct_marketing_low_price_headroom_enable',
        'direct_marketing_low_price_no_export',
        'direct_marketing_keep_headroom_pct',
        'direct_marketing_negative_price_charge_target_soc_pct',
        'direct_marketing_low_price_curtail_enable',
        'direct_marketing_low_price_curtail_limit_w',
        'direct_marketing_market_value_solar_enable',
        'direct_marketing_market_value_solar_source',
        'direct_marketing_aux_inverter_shelly_override',
        'direct_marketing_aux_inverter_shelly_ip',
        'direct_marketing_aux_inverter_shelly_invert',
        'direct_marketing_aux_inverter_shelly_dynamic_unblock_enable',
        'direct_marketing_aux_inverter_shelly_unblock_threshold_w',
        'direct_marketing_aux_inverter_shelly_contract_status',
        'direct_marketing_aux_inverter_shelly_contract_reason',
        'netztransparenz_client_id',
        'netztransparenz_client_secret',
        'direct_marketing_eeg_enable',
        'direct_marketing_eeg_commissioning_date',
        'direct_marketing_eeg_support_years',
        'direct_marketing_eeg_rate_source',
        'direct_marketing_eeg_system_type',
        'direct_marketing_eeg_feed_type',
        'direct_marketing_eeg_compensation_basis',
        'direct_marketing_eeg_grid_export_risk_ack',
        'tariff_provider',
        'stromtarif_typ',
        'speichergroesse',
        'maximumladeleistung',
        'maximaleentladeleistung',
        'einspeiselimit',
    ];
    $subset = [];
    foreach ($keys as $key) {
        if (array_key_exists($key, $config)) {
            $subset[$key] = installCenterRedactConfigValue($key, $config[$key]);
        }
    }
    return [
        'loaded' => !empty($config),
        'values' => $subset,
        'privacy_note' => 'Nur Direktvermarktungs-, Tarif- und technische Grenzwerte; sensible Werte bleiben redigiert.',
    ];
}

function installCenterDirectMarketingCompactWindow($window) {
    if (!is_array($window)) return null;
    $keys = [
        'start_t', 'end_t', 'action', 'reason', 'storage_action',
        'avg_market_ct', 'avg_billing_ct', 'net_sell_ct',
        'expected_profit_ct_per_kwh', 'max_power_w', 'target_soc_pct',
        'reserve_floor_soc_pct', 'pv_store_threshold_ct',
        'pv_store_threshold_source', 'pv_store_min_surplus_w',
        'pv_store_import_guard_w',
        'pv_store_min_hold_s', 'pv_store_ramp_step_w',
        'pv_store_dc_only_enable', 'pv_store_external_ac_guard_w',
        'pv_store_export_limit_guard_w', 'pv_store_export_limit_ramp_bypass_w',
        'negative_headroom_limited', 'negative_headroom_window_min',
        'negative_headroom_forecast_surplus_wh', 'negative_headroom_required_pct',
    ];
    $out = [];
    foreach ($keys as $key) {
        if (array_key_exists($key, $window)) $out[$key] = $window[$key];
    }
    foreach (['start_ts', 'end_ts'] as $key) {
        if (isset($window[$key]) && is_numeric($window[$key])) {
            $ts = (float)$window[$key];
            if ($ts > 10000000000) $ts = $ts / 1000.0;
            $out[$key . '_iso'] = date('c', (int)$ts);
        }
    }
    return $out;
}

function installCenterDirectMarketingWindowSummary($windows) {
    $rows = is_array($windows) ? array_values(array_filter($windows, 'is_array')) : [];
    $actionCounts = [];
    foreach ($rows as $row) {
        $action = (string)($row['action'] ?? 'unknown');
        $actionCounts[$action] = ($actionCounts[$action] ?? 0) + 1;
    }
    return [
        'count' => count($rows),
        'action_counts' => $actionCounts,
        'first_windows' => array_values(array_filter(array_map(
            'installCenterDirectMarketingCompactWindow',
            array_slice($rows, 0, 8)
        ))),
    ];
}

function installCenterDirectMarketingFileMeta($path) {
    $meta = installCenterFileMeta($path);
    if (!empty($meta['exists']) && isset($meta['modified_at'])) {
        $mtime = @filemtime($path);
        $meta['age_s'] = is_numeric($mtime) ? max(0, time() - (int)$mtime) : null;
    }
    return $meta;
}

function installCenterDirectMarketingDiagnosticsStatus() {
    $files = [
        'live_data_py' => '/var/www/html/ramdisk/live_data_py.json',
        'storage_plan' => '/var/www/html/ramdisk/storage_plan.json',
        'storage_manager_state' => '/var/www/html/ramdisk/storage_manager_state.json',
        'storage_decision_latest' => '/var/www/html/ramdisk/storage_decision_latest.json',
        'direct_marketing_daily_report' => '/var/www/html/ramdisk/direct_marketing_daily_report.json',
        'direct_marketing_aux_inverter_shelly' => '/var/www/html/ramdisk/direct_marketing_aux_inverter_shelly_state.json',
        'direct_marketing_aux_inverter_shelly_guard' => '/var/www/html/data/direct_marketing_aux_inverter_shelly_guard_state.json',
        'direct_marketing_aux_inverter_shelly_manual_lock' => '/var/www/html/data/direct_marketing_aux_inverter_shelly_manual_lock.json',
        'direct_marketing_aux_inverter_shelly_migration' => '/var/www/html/data/direct_marketing_aux_inverter_shelly_migration.json',
        'market_value_solar' => '/var/www/html/ramdisk/market_value_solar.json',
        'wb_pv_budget' => '/var/www/html/ramdisk/wb_pv_budget.json',
        'wallbox_storage_intent' => '/var/www/html/ramdisk/wallbox_storage_intent.json',
        'epex_daten' => '/var/www/html/ramdisk/epex_daten.json',
        'pv_forecast' => '/var/www/html/ramdisk/pv_forecast.json',
        'config_validation' => '/var/www/html/ramdisk/config_validation.json',
    ];
    $meta = [];
    $missing = [];
    foreach ($files as $key => $path) {
        $meta[$key] = installCenterDirectMarketingFileMeta($path);
        if (empty($meta[$key]['exists'])) $missing[] = $key;
    }

    $live = installCenterReadJsonAssoc($files['live_data_py']);
    $planFile = installCenterReadJsonAssoc($files['storage_plan']);
    $state = installCenterReadJsonAssoc($files['storage_manager_state']);
    $latest = installCenterReadJsonAssoc($files['storage_decision_latest']);
    if (isset($latest['decision']) && is_array($latest['decision'])) {
        $latest = array_replace($latest, $latest['decision']);
        if (!array_key_exists('val', $latest) && array_key_exists('val_w', $latest)) {
            $latest['val'] = $latest['val_w'];
        }
    }
    $reportFile = installCenterReadJsonAssoc($files['direct_marketing_daily_report']);
    $auxShelly = installCenterReadJsonAssoc($files['direct_marketing_aux_inverter_shelly']);
    $auxShellyGuard = installCenterReadJsonAssoc($files['direct_marketing_aux_inverter_shelly_guard']);
    $auxShellyManualLock = installCenterReadJsonAssoc($files['direct_marketing_aux_inverter_shelly_manual_lock']);
    $auxShellyMigration = installCenterReadJsonAssoc($files['direct_marketing_aux_inverter_shelly_migration']);
    $marketValueSolar = installCenterReadJsonAssoc($files['market_value_solar']);
    $wbBudget = installCenterReadJsonAssoc($files['wb_pv_budget']);
    $validation = installCenterReadJsonAssoc($files['config_validation']);
    $configSubset = installCenterDirectMarketingConfigSubset();
    $configValues = (isset($configSubset['values']) && is_array($configSubset['values'])) ? $configSubset['values'] : [];
    $marketValueSolarEnabledRaw = $configValues['direct_marketing_market_value_solar_enable'] ?? false;
    $marketValueSolarEnabled = is_bool($marketValueSolarEnabledRaw)
        ? $marketValueSolarEnabledRaw
        : in_array(strtolower(trim((string)$marketValueSolarEnabledRaw)), ['1', 'true', 'yes', 'on', 'ja', 'ein', 'aktiv'], true);

    $plan = (isset($planFile['direct_marketing']) && is_array($planFile['direct_marketing'])) ? $planFile['direct_marketing'] : [];
    $monitor = [];
    foreach ([$state, $latest, $wbBudget] as $source) {
        if (isset($source['direct_marketing_monitor']) && is_array($source['direct_marketing_monitor'])) {
            $monitor = $source['direct_marketing_monitor'];
            break;
        }
    }
    $report = $reportFile;
    if (!$report && isset($state['direct_marketing_daily_report']) && is_array($state['direct_marketing_daily_report'])) {
        $report = $state['direct_marketing_daily_report'];
    }

    $currentWindow = isset($latest['direct_marketing_window']) && is_array($latest['direct_marketing_window'])
        ? installCenterDirectMarketingCompactWindow($latest['direct_marketing_window'])
        : null;
    $blockedReasons = [];
    foreach ([
        $plan['blocked_reasons'] ?? [],
        $monitor['blocked_reasons'] ?? [],
        $latest['direct_marketing_blocked_reasons'] ?? [],
    ] as $reasons) {
        if (!is_array($reasons)) continue;
        foreach ($reasons as $reason) {
            $reason = trim((string)$reason);
            if ($reason !== '') $blockedReasons[$reason] = true;
        }
    }

    $priceValidation = [];
    if (isset($validation['price']) && is_array($validation['price'])) {
        foreach ($validation['price'] as $key => $entry) {
            if (str_starts_with((string)$key, 'direct_marketing_') && is_array($entry)) {
                $priceValidation[$key] = installCenterRedactConfigValue($key, $entry);
            }
        }
    }

    $planFlags = (isset($plan['flags']) && is_array($plan['flags'])) ? $plan['flags'] : [];
    $planEconomics = (isset($plan['economics']) && is_array($plan['economics'])) ? $plan['economics'] : [];
    $settlementAccounting = (isset($plan['settlement_accounting']) && is_array($plan['settlement_accounting'])) ? $plan['settlement_accounting'] : [];
    $batteryWearBudget = (isset($plan['battery_wear_budget']) && is_array($plan['battery_wear_budget'])) ? $plan['battery_wear_budget'] : [];
    $policyDecision = (isset($plan['policy_decision']) && is_array($plan['policy_decision'])) ? $plan['policy_decision'] : [];
    $policyTimeline = (isset($plan['policy_timeline']) && is_array($plan['policy_timeline'])) ? $plan['policy_timeline'] : [];
    $policyEconomics = (isset($policyDecision['economics']) && is_array($policyDecision['economics'])) ? $policyDecision['economics'] : [];
    $reserve = (isset($plan['reserve']) && is_array($plan['reserve'])) ? $plan['reserve'] : [];
    $planWindows = (isset($plan['windows']) && is_array($plan['windows'])) ? $plan['windows'] : [];
    $reportWindows = (isset($report['windows']) && is_array($report['windows'])) ? $report['windows'] : [];

    $notes = [];
    if (!$plan) $notes[] = 'storage_plan_without_direct_marketing_contract';
    if ($plan && empty($planWindows)) $notes[] = 'no_direct_marketing_windows';
    if (!empty($plan['shadow']) || !empty($monitor['shadow'])) $notes[] = 'shadow_only_no_commands';
    if (($planFlags['commands_allowed'] ?? null) === false) $notes[] = 'commands_not_allowed';
    if (!empty($planFlags['export_enable']) || !empty($planFlags['grid_charge_enable'])) $notes[] = 'active_export_or_grid_charge_flags_require_user_ack';
    if (in_array('epex_daten', $missing, true)) $notes[] = 'missing_price_file';
    if ($marketValueSolarEnabled && in_array('market_value_solar', $missing, true)) $notes[] = 'missing_market_value_solar_file';

    return [
        'schema_version' => 'direct_marketing_diagnosis_v1',
        'created_at' => date('c'),
        'status' => [
            'enabled' => (bool)($plan['active'] ?? $monitor['enabled'] ?? false),
            'shadow' => (bool)($plan['shadow'] ?? $monitor['shadow'] ?? false),
            'mode' => $plan['mode'] ?? ($latest['direct_marketing_mode'] ?? null),
            'reason' => $plan['reason'] ?? null,
            'controller_owner' => $plan['controller_owner'] ?? null,
            'plan_owner' => $plan['plan_owner'] ?? ($latest['direct_marketing_owner'] ?? null),
            'contract_version' => $plan['owner_contract_version'] ?? ($latest['direct_marketing_contract_version'] ?? null),
            'current_state' => $latest['state'] ?? ($state['state'] ?? null),
            'current_action' => $latest['direct_marketing_action'] ?? ($monitor['current_action'] ?? null),
            'commands_allowed' => $planFlags['commands_allowed'] ?? ($monitor['commands_allowed'] ?? null),
            'blocked_reasons' => array_keys($blockedReasons),
            'notes' => array_values(array_unique($notes)),
        ],
        'config' => $configSubset,
        'plan' => [
            'created_ts' => $plan['created_ts'] ?? null,
            'valid_until_ts' => $plan['valid_until_ts'] ?? null,
            'reserve' => $reserve,
            'flags' => installCenterRedactConfigValue('direct_marketing_flags', $planFlags),
            'economics' => $planEconomics,
            'settlement_accounting' => installCenterRedactConfigValue('direct_marketing_settlement_accounting', $settlementAccounting),
            'battery_wear_budget' => installCenterRedactConfigValue('direct_marketing_battery_wear_budget', $batteryWearBudget),
            'policy_decision' => installCenterRedactConfigValue('direct_marketing_policy_decision', $policyDecision),
            'policy_timeline' => installCenterRedactConfigValue('direct_marketing_policy_timeline', $policyTimeline),
            'policy_economics' => installCenterRedactConfigValue('direct_marketing_policy_economics', $policyEconomics),
            'windows' => installCenterDirectMarketingWindowSummary($planWindows),
        ],
        'runtime' => [
            'monitor' => installCenterRedactConfigValue('direct_marketing_monitor', $monitor),
            'current_window' => $currentWindow,
            'aux_inverter_shelly' => installCenterRedactConfigValue(
                'direct_marketing_aux_inverter_shelly',
                !empty($auxShelly) ? $auxShelly : ($state['direct_marketing_aux_inverter_shelly'] ?? null)
            ),
            'aux_inverter_shelly_guard' => installCenterRedactConfigValue('direct_marketing_aux_inverter_shelly_guard', $auxShellyGuard),
            'aux_inverter_shelly_manual_lock' => installCenterRedactConfigValue('direct_marketing_aux_inverter_shelly_manual_lock', $auxShellyManualLock),
            'aux_inverter_shelly_migration' => installCenterRedactConfigValue('direct_marketing_aux_inverter_shelly_migration', $auxShellyMigration),
            'latest_decision' => [
                'state' => $latest['state'] ?? null,
                'mode' => $latest['mode'] ?? null,
                'val' => $latest['val'] ?? null,
                'reason' => $latest['reason'] ?? null,
                'direct_marketing_active' => $latest['direct_marketing_active'] ?? null,
                'direct_marketing_policy_active' => $latest['direct_marketing_policy_active'] ?? null,
                'direct_marketing_policy_schema' => $latest['direct_marketing_policy_schema'] ?? null,
                'direct_marketing_policy_target_state' => $latest['direct_marketing_policy_target_state'] ?? null,
                'direct_marketing_policy_block_reason' => $latest['direct_marketing_policy_block_reason'] ?? null,
                'direct_marketing_policy_export_budget_w' => $latest['direct_marketing_policy_export_budget_w'] ?? null,
                'direct_marketing_policy_charge_budget_w' => $latest['direct_marketing_policy_charge_budget_w'] ?? null,
                'direct_marketing_policy_protected_reserve_wh' => $latest['direct_marketing_policy_protected_reserve_wh'] ?? null,
                'direct_marketing_policy_sellable_wh' => $latest['direct_marketing_policy_sellable_wh'] ?? null,
                'direct_marketing_policy_decision' => installCenterRedactConfigValue(
                    'direct_marketing_policy_decision',
                    (isset($latest['direct_marketing_policy_decision']) && is_array($latest['direct_marketing_policy_decision']))
                        ? $latest['direct_marketing_policy_decision']
                        : null
                ),
                'direct_marketing_export_execution' => installCenterRedactConfigValue(
                    'direct_marketing_export_execution',
                    (isset($latest['direct_marketing_export_execution']) && is_array($latest['direct_marketing_export_execution']))
                        ? $latest['direct_marketing_export_execution']
                        : null
                ),
                'rscp_power_settings' => installCenterRedactConfigValue(
                    'rscp_power_settings',
                    (isset($latest['rscp_power_settings']) && is_array($latest['rscp_power_settings']))
                        ? $latest['rscp_power_settings']
                        : null
                ),
                'direct_marketing_headroom_hold_active' => $latest['direct_marketing_headroom_hold_active'] ?? null,
                'direct_marketing_headroom_soc_ceiling_pct' => $latest['direct_marketing_headroom_soc_ceiling_pct'] ?? null,
                'direct_marketing_headroom_next_start_ts' => $latest['direct_marketing_headroom_next_start_ts'] ?? null,
                'direct_marketing_headroom_forecast_surplus_wh' => $latest['direct_marketing_headroom_forecast_surplus_wh'] ?? null,
                'direct_marketing_pv_store_w' => $latest['direct_marketing_pv_store_w'] ?? null,
                'direct_marketing_pv_store_export_limit_active' => $latest['direct_marketing_pv_store_export_limit_active'] ?? null,
                'direct_marketing_pv_store_export_limit_guard_active' => $latest['direct_marketing_pv_store_export_limit_guard_active'] ?? null,
                'direct_marketing_pv_store_export_limit_w' => $latest['direct_marketing_pv_store_export_limit_w'] ?? null,
                'direct_marketing_pv_store_export_limit_guard_w' => $latest['direct_marketing_pv_store_export_limit_guard_w'] ?? null,
                'direct_marketing_pv_store_export_over_limit_w' => $latest['direct_marketing_pv_store_export_over_limit_w'] ?? null,
                'direct_marketing_pv_store_export_absorb_target_w' => $latest['direct_marketing_pv_store_export_absorb_target_w'] ?? null,
                'direct_marketing_pv_store_unavoidable_export_w' => $latest['direct_marketing_pv_store_unavoidable_export_w'] ?? null,
                'direct_marketing_pv_store_export_limit_ramp_bypass' => $latest['direct_marketing_pv_store_export_limit_ramp_bypass'] ?? null,
                'direct_marketing_export_w' => $latest['direct_marketing_export_w'] ?? null,
                'direct_marketing_export_grid_import_w' => $latest['direct_marketing_export_grid_import_w'] ?? null,
                'direct_marketing_ramp_limited' => $latest['direct_marketing_ramp_limited'] ?? null,
                'direct_marketing_hold_active' => $latest['direct_marketing_hold_active'] ?? null,
            ],
            'live_context' => [
                'soc' => $live['SOC'] ?? null,
                'pv_w' => $live['PV_Power_Decision'] ?? ($live['PV_Power'] ?? null),
                'grid_w' => $live['Grid_Power_Decision'] ?? ($live['Grid_Power'] ?? null),
                'battery_w' => $live['Battery_Power_Decision'] ?? ($live['Battery_Power'] ?? null),
                'home_w' => $live['Home_Power_Decision'] ?? ($live['Home_Power'] ?? null),
                'home_power_source' => $live['home_power_source'] ?? ($live['Home_Power_Source'] ?? null),
                'home_power_valid' => $live['home_power_valid'] ?? ($live['Home_Power_Valid'] ?? null),
                'home_balance_w' => $live['home_balance_w'] ?? ($live['Home_Balance_W'] ?? null),
                'home_delta_w' => $live['home_delta_w'] ?? ($live['Home_Delta_W'] ?? null),
                'grid_power_valid' => $live['grid_power_valid'] ?? ($live['Grid_Power_Valid'] ?? null),
                'grid_pm_delta_w' => $live['Grid_PM_Delta'] ?? null,
                'grid_pm_sum_w' => $live['grid_pm_sum_w'] ?? null,
                'wallbox_w' => $live['Wallbox_Power_Decision'] ?? ($live['Wallbox_Power'] ?? null),
                'installed_peak_power_kwp' => $live['installed_peak_power_kwp'] ?? null,
                'installed_peak_power_source' => $live['installed_peak_power_source'] ?? null,
            ],
        ],
        'daily_report' => [
            'present' => !empty($report),
            'summary' => [
                'shadow_cycles' => $report['shadow_cycles'] ?? null,
                'active_cycles' => $report['active_cycles'] ?? null,
                'theoretical_export_kwh' => $report['theoretical_export_kwh'] ?? null,
                'theoretical_pv_store_kwh' => $report['theoretical_pv_store_kwh'] ?? null,
                'theoretical_window_profit_eur' => $report['theoretical_window_profit_eur'] ?? null,
                'window_action_counts' => $report['window_action_counts'] ?? null,
                'blocker_counts' => $report['blocker_counts'] ?? null,
            ],
            'windows' => installCenterDirectMarketingWindowSummary($reportWindows),
        ],
        'market_value_solar' => [
            'present' => !empty($marketValueSolar),
            'report' => installCenterRedactConfigValue('market_value_solar', $marketValueSolar),
        ],
        'validation' => [
            'price_direct_marketing' => $priceValidation,
        ],
        'files' => $meta,
        'missing' => $missing,
        'privacy_note' => 'Direktvermarktungsdiagnose: Konfiguration, Wirtschaftlichkeit, Fenster, Owner, Blocker und Live-Kontext; sensible Konfigwerte werden redigiert.',
    ];
}

function installCenterVersionFileValue() {
    global $install_path;
    $candidates = [
        ['path' => '/var/www/html/VERSION', 'source' => 'web_root/VERSION'],
    ];
    $installRoot = rtrim((string)$install_path, '/');
    if ($installRoot !== '') {
        $candidates[] = ['path' => $installRoot . '/VERSION', 'source' => 'install_path/VERSION'];
    }
    foreach ($candidates as $candidate) {
        $path = (string)$candidate['path'];
        if (!is_readable($path)) continue;
        $version = trim((string)@file_get_contents($path));
        if ($version !== '') {
            return ['version' => $version, 'source' => (string)$candidate['source']];
        }
    }
    return ['version' => '', 'source' => 'not_found'];
}

function installCenterInstallerPackageVersion() {
    global $install_path;
    $initFile = rtrim((string)$install_path, '/') . '/Installer/__init__.py';
    if (!is_readable($initFile)) return '';
    $source = (string)@file_get_contents($initFile);
    if (preg_match('/__version__\s*=\s*[\'"]([^\'"]+)[\'"]/', $source, $matches) === 1) {
        return trim((string)$matches[1]);
    }
    return '';
}

function installCenterNormalizeGitHash($hash) {
    $hash = trim((string)$hash);
    return preg_match('/^[0-9a-f]{7,40}$/i', $hash) === 1 ? $hash : null;
}

function installCenterPackedRefs($gitDir) {
    $packedRefsFile = rtrim((string)$gitDir, '/') . '/packed-refs';
    if (!is_readable($packedRefsFile)) return [];
    $lines = @file($packedRefsFile, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    if (!is_array($lines)) return [];

    $refs = [];
    $lastRef = null;
    foreach ($lines as $line) {
        $line = trim((string)$line);
        if ($line === '' || $line[0] === '#') continue;
        if ($line[0] === '^') {
            if ($lastRef !== null) {
                $peeled = installCenterNormalizeGitHash(substr($line, 1));
                if ($peeled) $refs[$lastRef]['peeled'] = $peeled;
            }
            continue;
        }
        $parts = preg_split('/\s+/', $line, 2);
        if (count($parts) !== 2) {
            $lastRef = null;
            continue;
        }
        $hash = installCenterNormalizeGitHash($parts[0]);
        $ref = trim((string)$parts[1]);
        if (!$hash || $ref === '') {
            $lastRef = null;
            continue;
        }
        $refs[$ref] = ['hash' => $hash, 'peeled' => null];
        $lastRef = $ref;
    }
    return $refs;
}

function installCenterReadGitRef($repoDir, $refPath) {
    $repoDir = rtrim((string)$repoDir, '/');
    $refPath = trim((string)$refPath);
    if ($repoDir === '' || $refPath === '' || strpos($refPath, '..') !== false) return null;

    $gitDir = $repoDir . '/.git';
    if (!is_dir($gitDir)) return null;

    $refFile = $gitDir . '/' . $refPath;
    if (is_readable($refFile)) {
        $hash = installCenterNormalizeGitHash((string)@file_get_contents($refFile));
        if ($hash) return $hash;
    }

    $packedRefs = installCenterPackedRefs($gitDir);
    return $packedRefs[$refPath]['hash'] ?? null;
}

function installCenterGitHeadMetadata($repoDir) {
    $repoDir = rtrim((string)$repoDir, '/');
    $gitDir = $repoDir . '/.git';
    $headFile = $gitDir . '/HEAD';
    if ($repoDir === '' || !is_dir($gitDir) || !is_readable($headFile)) {
        return ['available' => false, 'branch' => null, 'commit' => null, 'commit_full' => null];
    }

    $head = trim((string)@file_get_contents($headFile));
    if ($head === '') return ['available' => false, 'branch' => null, 'commit' => null, 'commit_full' => null];

    if (strpos($head, 'ref:') === 0) {
        $refPath = trim(substr($head, 4));
        $branch = $refPath;
        if (strpos($refPath, 'refs/heads/') === 0) {
            $branch = substr($refPath, strlen('refs/heads/'));
        }
        $commit = installCenterReadGitRef($repoDir, $refPath);
        return [
            'available' => $commit !== null,
            'branch' => $branch !== '' ? $branch : null,
            'commit' => $commit ? substr($commit, 0, 12) : null,
            'commit_full' => $commit,
        ];
    }

    $commit = installCenterNormalizeGitHash($head);
    return [
        'available' => $commit !== null,
        'branch' => $commit ? 'HEAD' : null,
        'commit' => $commit ? substr($commit, 0, 12) : null,
        'commit_full' => $commit,
    ];
}

function installCenterExactTagForCommit($repoDir, $commitHash) {
    $repoDir = rtrim((string)$repoDir, '/');
    $commitHash = installCenterNormalizeGitHash($commitHash);
    $gitDir = $repoDir . '/.git';
    if (!$commitHash || !is_dir($gitDir)) return null;

    $tagRoot = $gitDir . '/refs/tags';
    if (is_dir($tagRoot)) {
        $iterator = new RecursiveIteratorIterator(
            new RecursiveDirectoryIterator($tagRoot, FilesystemIterator::SKIP_DOTS)
        );
        foreach ($iterator as $fileInfo) {
            if (!$fileInfo->isFile() || !$fileInfo->isReadable()) continue;
            $tagHash = installCenterNormalizeGitHash((string)@file_get_contents($fileInfo->getPathname()));
            if ($tagHash && strcasecmp($tagHash, $commitHash) === 0) {
                $relative = substr($fileInfo->getPathname(), strlen($tagRoot) + 1);
                return str_replace(DIRECTORY_SEPARATOR, '/', $relative);
            }
        }
    }

    foreach (installCenterPackedRefs($gitDir) as $ref => $entry) {
        if (strpos($ref, 'refs/tags/') !== 0) continue;
        $matchesCommit = strcasecmp((string)($entry['hash'] ?? ''), $commitHash) === 0
            || strcasecmp((string)($entry['peeled'] ?? ''), $commitHash) === 0;
        if ($matchesCommit) return substr($ref, strlen('refs/tags/'));
    }
    return null;
}

function installCenterVersionMetadata() {
    global $install_path;
    $versionInfo = installCenterVersionFileValue();
    $repoDir = rtrim((string)$install_path, '/');
    $gitHead = installCenterGitHeadMetadata($repoDir);
    $gitCommit = $gitHead['commit'] ?? null;
    $gitCommitFull = $gitHead['commit_full'] ?? null;

    return [
        'schema_version' => 'e3dc_diagnose_version_v1',
        'created_at' => date('c'),
        'version' => (string)$versionInfo['version'],
        'version_source' => (string)$versionInfo['source'],
        'installer_package_version' => installCenterInstallerPackageVersion(),
        'git' => [
            'available' => (bool)($gitHead['available'] ?? false),
            'branch' => $gitHead['branch'] ?? null,
            'commit' => $gitCommit,
            'exact_tag' => installCenterExactTagForCommit($repoDir, $gitCommitFull),
            'dirty' => null,
            'dirty_count' => null,
        ],
        'runtime' => [
            'docker_detected' => is_file('/.dockerenv') || getenv('E3DC_WEB_PORT') !== false || getenv('E3DC_WEB_BIND') !== false,
            'php_version' => PHP_VERSION,
        ],
        'privacy_note' => 'Versions- und Git-Metadaten enthalten keine Zugangsdaten und keine Remote-URL.',
    ];
}

function installCenterDiagnosticCandidates() {
    $defaultLogs = [
        'storage_manager.log',
        'wallbox_manager.log',
        'energy_manager.log',
        'storage_simulator.log',
        'e3dc_live.log',
        'e3dc_mqtt_hub.log',
        'pv_forecast.log',
    ];
    $compactLogBytes = 90000;
    $items = [
        [
            'id' => 'config:redacted',
            'label' => 'Bereinigte Konfiguration',
            'kind' => 'config',
            'path' => '/var/www/html/data/e3dc_v4.json',
            'bundle_size' => 30000,
            'default' => true,
            'privacy' => 'Passwörter, Tokens, E-Mail und Standortwerte werden maskiert.'
        ],
        [
            'id' => 'status:installer',
            'label' => 'Installer-Status',
            'kind' => 'status',
            'path' => 'web_installer.py --action installer_status',
            'bundle_size' => 30000,
            'default' => true,
            'privacy' => 'Statusdaten, keine Passwörter.'
        ],
        [
            'id' => 'status:diagnosis',
            'label' => 'Modul-Diagnose komplett',
            'kind' => 'status',
            'path' => 'web_installer.py --action diagnosis',
            'bundle_size' => 140000,
            'default' => true,
            'privacy' => 'Status, Alive-Dateien, letzte Log-/Journal-Zeilen; Text wird zusätzlich bereinigt.'
        ],
        [
            'id' => 'status:job',
            'label' => 'Letzter Web-Installer-Job',
            'kind' => 'status',
            'path' => '/var/www/html/ramdisk/web_install_status.json',
            'bundle_size' => 15000,
            'default' => true,
            'privacy' => 'Hilft bei Installer-Fehlern.'
        ],
        [
            'id' => 'status:ml_docker',
            'label' => 'ML-/Docker-Prognose-Status',
            'kind' => 'status',
            'path' => 'Privater Modellstatus, ml_prediction.json, Docker-Warmstartcache',
            'bundle_size' => 30000,
            'default' => true,
    'privacy' => 'Privates Modell und Speicherpfad bleiben für den Webprozess unlesbar; kein Modellinhalt.'
        ],
        [
            'id' => 'status:power_decision',
            'label' => 'EWMA-/Glitch-Diagnose',
            'kind' => 'status',
            'path' => 'live_decision_stability.json, live_data_py.json, live_plausibility_glitches_*.jsonl',
            'bundle_size' => 50000,
            'default' => true,
            'privacy' => 'Kompakte Leistungsdiagnose und Plausibilitätsgründe; keine Zugangsdaten.'
        ],
        [
            'id' => 'status:glitch_situations',
            'label' => 'Glitch-Situationsanalyse',
            'kind' => 'status',
            'path' => 'glitch_situation_summary.json, live_plausibility_glitches_*.jsonl, live_plausibility_glitches_*.jsonl.gz',
            'bundle_size' => 50000,
            'default' => true,
            'privacy' => 'Maschinenlesbare Auswertung nach Wallbox-, PV-, Netz-, Batterie- und Burst-Kontext.'
        ],
        [
            'id' => 'status:incident_timeline',
            'label' => 'Vorfalls-Timeline',
            'kind' => 'status',
            'path' => 'Entscheidungs-, Glitch- und Wallbox-Befehlsereignisse im gewählten Zeitfenster',
            'bundle_size' => 180000,
            'default' => true,
            'privacy' => 'Zusammengeführte JSONL-Timeline; Fahrzeug-, Netzwerk- und Topic-Identitäten werden pseudonymisiert.'
        ],
        [
            'id' => 'status:direct_marketing',
            'label' => 'Direktvermarktungs-Diagnose',
            'kind' => 'status',
            'path' => 'storage_plan.json, storage_manager_state.json, direct_marketing_daily_report.json, market_value_solar.json, epex_daten.json',
            'bundle_size' => 80000,
            'default' => false,
            'privacy' => 'Kompakter DV-Status mit Fenstern, Blockern, Wirtschaftlichkeit und Live-Kontext; sensible Konfigwerte werden maskiert.'
        ],
    ];
    foreach (glob('/var/www/html/logs/*') ?: [] as $path) {
        if (!is_file($path)) continue;
        $base = basename($path);
        if (!preg_match('/\.(log|txt|json|jsonl|jsonl\.gz)$/i', $base)) continue;
        $isMachineLog = installCenterDiagnosticIsKnownMachineJsonlName($base);
        $size = @filesize($path) ?: 0;
        $items[] = [
            'id' => 'log:' . $base,
            'label' => 'Log: ' . $base,
            'kind' => 'log',
            'path' => $path,
            'size' => $size,
            'bundle_size' => min($size, $compactLogBytes),
            'default' => in_array($base, $defaultLogs, true),
            'privacy' => $isMachineLog
                ? 'Maschinenlesbarer, strukturierter und redigierter JSONL-Ausschnitt; keine unveränderte Rohhistorie.'
                : 'Text wird bereinigt; im Standardpaket werden nur die letzten ca. 90 KB gepackt.'
        ];
    }
    $ramdiskFiles = [
        'live_data_py.json',
        'live_decision_stability.json',
        'live_data_last_valid.json',
        'storage_plan.json',
        'storage_manager_state.json',
        'storage_decision_latest.json',
        'ems_decision_latest.json',
        'direct_marketing_daily_report.json',
        'direct_marketing_aux_inverter_shelly_state.json',
        'market_value_solar.json',
        'wb_pv_budget.json',
        'wb_pv_budget_diagnostics.json',
        'wallbox_storage_intent.json',
        'energy_decision_latest.json',
        'wallbox_decision_latest.json',
        'config_validation.json',
        'mqtt_ha_inbound.json',
        'external_wb.json',
        'wallbox_native.json',
        'openwb_data.json',
        'openwb_data_wb2.json',
        'native_wallbox_schedule.json',
        'native_wallbox_schedule_wb1.json',
        'native_wallbox_schedule_wb2.json',
        'manual_soc_wb1.json',
        'manual_soc_wb2.json',
        'vehicles.json',
        'waermepumpe.json',
        'luxtronik.json',
        'luxtronik_stats.json',
        'luxtronik_history.json',
        'stiebel_isg.json',
        'dimplex_wpm.json',
        'heizstab_data.json',
        'climate_load.json',
        'climate_control.json',
        'epex_daten.json',
        'pv_forecast.json',
        'ml_prediction.json',
        'web_install_status.json',
        'web_install_jobs.json'
    ];
    foreach ($ramdiskFiles as $base) {
        $path = '/var/www/html/ramdisk/' . $base;
        if (!is_file($path)) continue;
        $items[] = [
            'id' => 'ramdisk:' . $base,
            'label' => 'Ramdisk: ' . $base,
            'kind' => 'ramdisk',
            'path' => $path,
            'size' => @filesize($path) ?: 0,
            'bundle_size' => min(@filesize($path) ?: 0, 250000),
            'default' => in_array($base, ['storage_plan.json', 'storage_manager_state.json', 'storage_decision_latest.json', 'ems_decision_latest.json', 'live_decision_stability.json', 'config_validation.json', 'web_install_status.json'], true),
            'privacy' => 'Live-/Planungsdaten, Text wird bereinigt.'
        ];
    }
    $auxMigrationPath = '/var/www/html/data/direct_marketing_aux_inverter_shelly_migration.json';
    if (is_file($auxMigrationPath)) {
        $items[] = [
            'id' => 'data:direct_marketing_aux_inverter_shelly_migration.json',
            'label' => 'Persistenter Zusatz-WR-Migrationsstatus',
            'kind' => 'data',
            'path' => $auxMigrationPath,
            'size' => @filesize($auxMigrationPath) ?: 0,
            'bundle_size' => min(@filesize($auxMigrationPath) ?: 0, 30000),
            'default' => false,
            'privacy' => 'Neutraler terminaler Migrationsstatus ohne historische Klartextnamen.'
        ];
    }
    return [
        'success' => true,
        'items' => $items,
        'presets' => installCenterDiagnosticPresets($items),
        'privacy_note' => 'Das Paket wird lokal erzeugt. Passwörter, Tokens, E-Mail-Adressen und Standortwerte werden maskiert. Die Standardauswahl ist kompakt und forumstauglich; weitere Dateien können bewusst zusätzlich ausgewählt werden.'
    ];
}

function installCenterDiagnosticItemIndex($items) {
    $index = [];
    foreach ($items as $item) {
        $id = (string)($item['id'] ?? '');
        if ($id !== '') $index[$id] = $item;
    }
    return $index;
}

function installCenterDiagnosticRecentIds($items, $pattern, $limit = 2) {
    $matches = [];
    foreach ($items as $item) {
        $id = (string)($item['id'] ?? '');
        if ($id !== '' && preg_match($pattern, $id)) $matches[] = $id;
    }
    sort($matches, SORT_NATURAL);
    if ($limit > 0 && count($matches) > $limit) {
        $matches = array_slice($matches, -$limit);
    }
    return $matches;
}

function installCenterDiagnosticBuildPreset($items, $id, $label, $icon, $description, $requestedIds, $options = []) {
    $index = installCenterDiagnosticItemIndex($items);
    $selected = [];
    $seen = [];
    foreach ($requestedIds as $requestedId) {
        $requestedId = (string)$requestedId;
        if ($requestedId === '' || isset($seen[$requestedId]) || !isset($index[$requestedId])) continue;
        $seen[$requestedId] = true;
        $selected[] = $requestedId;
    }
    $bundleSize = 0;
    foreach ($selected as $selectedId) {
        $bundleSize += (int)($index[$selectedId]['bundle_size'] ?? $index[$selectedId]['size'] ?? 0);
    }
    $forumLimit = (int)($options['forum_limit_bytes'] ?? 0);
    return [
        'id' => $id,
        'label' => $label,
        'icon' => $icon,
        'description' => $description,
        'items' => $selected,
        'item_count' => count($selected),
        'bundle_size' => $bundleSize,
        'forum_limit_bytes' => $forumLimit > 0 ? $forumLimit : null,
        'forum_safe' => $forumLimit > 0 ? ($bundleSize <= $forumLimit) : null,
    ];
}

function installCenterDiagnosticPresets($items) {
    $defaults = [];
    foreach ($items as $item) {
        if (!empty($item['default']) && !empty($item['id'])) $defaults[] = (string)$item['id'];
    }

    $base = [
        'config:redacted',
        'status:installer',
        'status:diagnosis',
        'status:power_decision',
        'status:glitch_situations',
        'status:incident_timeline',
        'ramdisk:config_validation.json',
        'ramdisk:live_data_py.json',
        'ramdisk:live_decision_stability.json',
        'ramdisk:ems_decision_latest.json',
    ];
    $storageRecent = installCenterDiagnosticRecentIds($items, '/^log:storage_decision_history_\d+\.jsonl\.gz$/', 2);
    $wallboxRecent = installCenterDiagnosticRecentIds($items, '/^log:wallbox_decision_history_\d+\.jsonl\.gz$/', 2);
    $energyRecent = installCenterDiagnosticRecentIds($items, '/^log:energy_decision_history_\d+\.jsonl\.gz$/', 2);
    $emsRecent = installCenterDiagnosticRecentIds($items, '/^log:ems_reaction_history_\d+\.jsonl\.gz$/', 2);
    $glitchRecent = installCenterDiagnosticRecentIds($items, '/^log:live_plausibility_glitches_\d+\.jsonl(?:\.gz)?$/', 2);
    $forumLimitBytes = 1024 * 1024;
    $forumCompactPreset = [
        'config:redacted',
        'status:installer',
        'status:diagnosis',
        'status:job',
        'status:power_decision',
        'status:glitch_situations',
        'status:incident_timeline',
        'ramdisk:config_validation.json',
        'ramdisk:live_data_py.json',
        'ramdisk:live_decision_stability.json',
        'ramdisk:storage_plan.json',
        'ramdisk:storage_decision_latest.json',
        'ramdisk:ems_decision_latest.json',
        'ramdisk:wallbox_decision_latest.json',
        'log:e3dc_live.log',
        'log:storage_manager.log',
        'log:wallbox_manager.log',
    ];
    $powerDecisionPreset = array_merge([
        'status:power_decision',
        'status:glitch_situations',
        'ramdisk:live_data_py.json',
        'ramdisk:live_decision_stability.json',
        'ramdisk:live_data_last_valid.json',
        'ramdisk:storage_decision_latest.json',
        'ramdisk:wallbox_decision_latest.json',
        'ramdisk:energy_decision_latest.json',
        'ramdisk:ems_decision_latest.json',
        'log:e3dc_live.log',
    ], $storageRecent, $wallboxRecent, $energyRecent, $glitchRecent);
    $directMarketingPreset = array_merge([
        'status:direct_marketing',
        'config:redacted',
        'ramdisk:config_validation.json',
        'ramdisk:live_data_py.json',
        'ramdisk:storage_plan.json',
        'ramdisk:storage_manager_state.json',
        'ramdisk:storage_decision_latest.json',
        'ramdisk:ems_decision_latest.json',
        'ramdisk:direct_marketing_daily_report.json',
        'ramdisk:direct_marketing_aux_inverter_shelly_state.json',
        'data:direct_marketing_aux_inverter_shelly_migration.json',
        'ramdisk:market_value_solar.json',
        'ramdisk:wb_pv_budget.json',
        'ramdisk:wallbox_storage_intent.json',
        'ramdisk:epex_daten.json',
        'ramdisk:pv_forecast.json',
        'log:storage_manager.log',
        'log:storage_simulator.log',
        'log:e3dc_live.log',
        'log:pv_forecast.log',
    ], $storageRecent, $wallboxRecent, $energyRecent, $glitchRecent);

    return [
        installCenterDiagnosticBuildPreset(
            $items,
            'standard',
            'Forum kompakt',
            'fa-shield-halved',
            'Kompakte Grunddiagnose unter 1 MB: Status, Summarys und kurze Log-Tails ohne Roh-Historien.',
            $forumCompactPreset,
            ['forum_limit_bytes' => $forumLimitBytes]
        ),
        installCenterDiagnosticBuildPreset(
            $items,
            'power_decision',
            'EWMA / Glitches Analyse',
            'fa-wave-square',
            'EWMA-Stabilität, Decision-Werte, redigierte Plausibilitäts- und Entscheidungshistorien sowie aktuelle Manager-Entscheidungen.',
            $powerDecisionPreset
        ),
        installCenterDiagnosticBuildPreset(
            $items,
            'direct_marketing',
            'Direktvermarktung',
            'fa-chart-line',
            'DV-Plan, PV-Speicher-/Exportfenster, Wirtschaftlichkeit, Owner, Blocker, EPEX/PV-Input und Live-Kontext.',
            $directMarketingPreset
        ),
        installCenterDiagnosticBuildPreset(
            $items,
            'wallbox',
            'Wallbox',
            'fa-charging-station',
            'Wallbox-Steuerung, openWB/go-e/E3DC, Ladefenster, Gate und Fahrzeug-SoC.',
            array_merge($base, [
                'log:wallbox_manager.log',
                'log:wallbox_command_audit.log',
                'log:storage_manager.log',
                'log:e3dc_live.log',
                'ramdisk:wallbox_native.json',
                'ramdisk:wallbox_decision_latest.json',
                'ramdisk:ems_decision_latest.json',
                'ramdisk:openwb_data.json',
                'ramdisk:openwb_data_wb2.json',
                'ramdisk:external_wb.json',
                'ramdisk:mqtt_ha_inbound.json',
                'ramdisk:wb_pv_budget.json',
                'ramdisk:wb_pv_budget_diagnostics.json',
                'ramdisk:wallbox_storage_intent.json',
                'ramdisk:native_wallbox_schedule.json',
                'ramdisk:native_wallbox_schedule_wb1.json',
                'ramdisk:native_wallbox_schedule_wb2.json',
                'ramdisk:manual_soc_wb1.json',
                'ramdisk:manual_soc_wb2.json',
                'ramdisk:vehicles.json',
                'ramdisk:storage_manager_state.json',
                'ramdisk:storage_decision_latest.json',
            ], $wallboxRecent, $storageRecent, $glitchRecent)
        ),
        installCenterDiagnosticBuildPreset(
            $items,
            'curve',
            'Ladekurve',
            'fa-route',
            'Speicherplanung, Kurvenführung, Abregelschutz, iFc und PV-Prognose.',
            array_merge($base, [
                'log:storage_manager.log',
                'log:storage_simulator.log',
                'log:pv_forecast.log',
                'log:e3dc_live.log',
                'ramdisk:storage_plan.json',
                'ramdisk:storage_manager_state.json',
                'ramdisk:storage_decision_latest.json',
                'ramdisk:ems_decision_latest.json',
                'ramdisk:wb_pv_budget.json',
                'ramdisk:wb_pv_budget_diagnostics.json',
                'ramdisk:wallbox_storage_intent.json',
                'ramdisk:wallbox_decision_latest.json',
                'ramdisk:pv_forecast.json',
                'ramdisk:ml_prediction.json',
                'ramdisk:epex_daten.json',
            ], $storageRecent, $emsRecent, $glitchRecent)
        ),
        installCenterDiagnosticBuildPreset(
            $items,
            'heatpump',
            'Wärmepumpe',
            'fa-temperature-three-quarters',
            'Luxtronik, iDM, Stiebel, Heizstab, PV-Boost, Pausen und Verbrauchswerte.',
            array_merge($base, [
                'log:energy_manager.log',
                'log:stiebel_live.log',
                'log:dimplex_live.log',
                'log:lux_live.log',
                'log:idm_live.log',
                'log:heizstab_manager.log',
                'log:climate_live.log',
                'log:climate_control.log',
                'log:storage_manager.log',
                'ramdisk:energy_decision_latest.json',
                'ramdisk:ems_decision_latest.json',
                'ramdisk:waermepumpe.json',
                'ramdisk:luxtronik.json',
                'ramdisk:luxtronik_stats.json',
                'ramdisk:luxtronik_history.json',
                'ramdisk:stiebel_isg.json',
                'ramdisk:dimplex_wpm.json',
                'ramdisk:heizstab_data.json',
                'ramdisk:climate_load.json',
                'ramdisk:climate_control.json',
                'ramdisk:storage_manager_state.json',
                'ramdisk:wb_pv_budget.json',
            ], $energyRecent, $glitchRecent)
        ),
        installCenterDiagnosticBuildPreset(
            $items,
            'forecast',
            'Prognose / ML',
            'fa-cloud-sun',
            'PV-Prognose, Wetter-/EPEX-Daten, ML-Modellstatus und Docker-Warmstart.',
            array_merge($base, [
                'status:ml_docker',
                'log:pv_forecast.log',
                'log:epex_manager.log',
                'log:forecast_model_cache.json',
                'log:pv_forecast_eval.json',
                'log:ml_consumption_eval.json',
                'ramdisk:pv_forecast.json',
                'ramdisk:ml_prediction.json',
                'ramdisk:epex_daten.json',
                'ramdisk:storage_plan.json',
            ])
        ),
        installCenterDiagnosticBuildPreset(
            $items,
            'mqtt_ha',
            'MQTT / HA',
            'fa-house',
            'Home Assistant, MQTT-Hub, externe Wallbox-/Verbraucherwerte und Fahrzeugdaten.',
            array_merge($base, [
                'log:e3dc_mqtt_hub.log',
                'log:ha_manager.log',
                'ramdisk:mqtt_ha_inbound.json',
                'ramdisk:external_wb.json',
                'ramdisk:vehicles.json',
                'ramdisk:wallbox_native.json',
                'ramdisk:waermepumpe.json',
            ])
        ),
        installCenterDiagnosticBuildPreset(
            $items,
            'installer',
            'Installation / Update',
            'fa-screwdriver-wrench',
            'Installer, Dienste, Rechte, Web-Update und letzter Installationsjob.',
            array_merge($defaults, [
                'status:job',
                'log:update.log',
                'log:self_update_php.log',
                'log:piguard.log',
                'ramdisk:web_install_jobs.json',
                'ramdisk:web_install_status.json',
            ])
        ),
    ];
}

function installCenterDiagnosticContentFormat($name) {
    $name = strtolower((string)$name);
    if (str_ends_with($name, '.gz')) {
        $name = substr($name, 0, -3);
    }
    if (str_ends_with($name, '.jsonl')) return 'jsonl';
    if (str_ends_with($name, '.json')) return 'json';
    return 'text';
}

function installCenterNormalizeMachineReadableText($text, $format, $tailMode = false) {
    $text = (string)$text;
    $format = (string)$format;
    if ($format === 'json') {
        $decoded = json_decode($text, true);
        if (json_last_error() === JSON_ERROR_NONE && is_array($decoded)) {
            return [
                'data' => json_encode(installCenterRedactConfigValue('', $decoded), JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) . "\n",
                'content_format' => 'json',
                'json_records' => 1,
                'invalid_json_lines' => 0,
                'dropped_partial_lines' => 0,
            ];
        }
        $format = 'text';
    }
    if ($format === 'jsonl') {
        $out = [];
        $invalid = 0;
        $dropped = 0;
        foreach (preg_split('/\r?\n/', $text) as $idx => $line) {
            $line = trim((string)$line);
            if ($line === '') continue;
            if ($tailMode && !$out && $idx === 0 && !str_starts_with($line, '{') && !str_starts_with($line, '[')) {
                $dropped++;
                continue;
            }
            $decoded = json_decode($line, true);
            if (json_last_error() !== JSON_ERROR_NONE || !is_array($decoded)) {
                $invalid++;
                continue;
            }
            $out[] = json_encode(installCenterRedactConfigValue('', $decoded), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        }
        return [
            'data' => $out ? implode("\n", $out) . "\n" : '',
            'content_format' => 'jsonl',
            'json_records' => count($out),
            'invalid_json_lines' => $invalid,
            'dropped_partial_lines' => $dropped,
        ];
    }
    return [
        'data' => installCenterRedactText($text),
        'content_format' => 'text',
        'json_records' => null,
        'invalid_json_lines' => null,
        'dropped_partial_lines' => null,
    ];
}

function installCenterDiagnosticMachineArchiveName($archiveName, $contentFormat, $truncated = false, $sourceCompressed = false) {
    $name = str_replace('\\', '/', trim((string)$archiveName, '/'));
    if ($name === '') $name = 'diagnose.txt';
    if ($sourceCompressed && str_ends_with(strtolower($name), '.gz')) {
        $name = substr($name, 0, -3);
    }
    $lower = strtolower($name);
    if ($contentFormat === 'jsonl') {
        if (str_ends_with($lower, '.jsonl')) {
            return $truncated ? substr($name, 0, -6) . '.tail.jsonl' : $name;
        }
        return $name . ($truncated ? '.tail.jsonl' : '.jsonl');
    }
    if ($contentFormat === 'json') {
        if (!$truncated && str_ends_with($lower, '.json')) return $name;
        if (str_ends_with($lower, '.json')) return substr($name, 0, -5) . '.tail.txt';
        return $name . ($truncated ? '.tail.txt' : '.json');
    }
    if (!$truncated) return $name;
    $slash = strrpos($name, '/');
    $dir = $slash === false ? '' : substr($name, 0, $slash + 1);
    $base = $slash === false ? $name : substr($name, $slash + 1);
    $dot = strrpos($base, '.');
    if ($dot === false) return $dir . $base . '.tail.txt';
    return $dir . substr($base, 0, $dot) . '.tail' . substr($base, $dot);
}

function installCenterDiagnosticIsKnownMachineJsonlName($name) {
    $base = basename((string)$name);
    return preg_match('/^(storage_decision_history|wallbox_decision_history|energy_decision_history|ems_reaction_history|live_plausibility_glitches)_\d+\.jsonl(?:\.gz)?$/', $base) === 1;
}

function installCenterReadDiagnosticFile($path, $archiveName, $maxBytes = 90000) {
    if (!is_file($path) || !is_readable($path)) {
        $name = installCenterDiagnosticMachineArchiveName($archiveName, 'text', false, false);
        return [
            'name' => $name,
            'data' => "Datei nicht lesbar: " . $path . "\n",
            'meta' => [
                'archive_name' => $name,
                'source_path' => $path,
                'content_format' => 'text',
                'readable' => false,
                'error' => 'not_readable',
            ],
        ];
    }
    $sourceSize = @filesize($path);
    $sourceSize = is_numeric($sourceSize) ? (int)$sourceSize : null;
    $sourceCompressed = str_ends_with((string)$path, '.gz');
    $sourceFormat = installCenterDiagnosticContentFormat($path);
    $truncated = false;
    $text = '';
    $decompressedBytes = null;
    if (str_ends_with($path, '.gz')) {
        if (!function_exists('gzopen')) {
            $name = installCenterDiagnosticMachineArchiveName($archiveName, 'text', false, true);
            return [
                'name' => $name,
                'data' => "Gzip-Datei kann auf diesem System nicht gelesen werden: " . $path . "\n",
                'meta' => [
                    'archive_name' => $name,
                    'source_path' => $path,
                    'source_size_bytes' => $sourceSize,
                    'source_compressed' => true,
                    'content_format' => 'text',
                    'readable' => false,
                    'error' => 'gzip_unavailable',
                ],
            ];
        }
        $handle = @gzopen($path, 'rb');
        if (!$handle) {
            $name = installCenterDiagnosticMachineArchiveName($archiveName, 'text', false, true);
            return [
                'name' => $name,
                'data' => "Gzip-Datei konnte nicht gelesen werden: " . $path . "\n",
                'meta' => [
                    'archive_name' => $name,
                    'source_path' => $path,
                    'source_size_bytes' => $sourceSize,
                    'source_compressed' => true,
                    'content_format' => 'text',
                    'readable' => false,
                    'error' => 'gzip_read_failed',
                ],
            ];
        }
        $lines = [];
        $bytes = 0;
        while (!gzeof($handle)) {
            $line = @gzgets($handle);
            if ($line === false) break;
            $lines[] = $line;
            $bytes += strlen($line);
            while ($bytes > $maxBytes && count($lines) > 1) {
                $removed = array_shift($lines);
                $bytes -= strlen($removed);
                $truncated = true;
            }
        }
        @gzclose($handle);
        $text = implode('', $lines);
        $decompressedBytes = $bytes;
    } else {
        $size = @filesize($path) ?: 0;
        $offset = max(0, $size - $maxBytes);
        $text = @file_get_contents($path, false, null, $offset);
        if ($text === false) {
            $name = installCenterDiagnosticMachineArchiveName($archiveName, 'text', false, false);
            return [
                'name' => $name,
                'data' => "Datei konnte nicht gelesen werden: " . $path . "\n",
                'meta' => [
                    'archive_name' => $name,
                    'source_path' => $path,
                    'source_size_bytes' => $sourceSize,
                    'source_compressed' => false,
                    'content_format' => 'text',
                    'readable' => false,
                    'error' => 'read_failed',
                ],
            ];
        }
        $truncated = $offset > 0;
    }
    $normalized = installCenterNormalizeMachineReadableText($text, $sourceFormat, $truncated);
    $contentFormat = (string)($normalized['content_format'] ?? 'text');
    $name = installCenterDiagnosticMachineArchiveName($archiveName, $contentFormat, $truncated, $sourceCompressed);
    return [
        'name' => $name,
        'data' => (string)($normalized['data'] ?? ''),
        'meta' => [
            'archive_name' => $name,
            'source_archive_name' => $archiveName,
            'source_path' => $path,
            'source_size_bytes' => $sourceSize,
            'source_compressed' => $sourceCompressed,
            'source_format' => $sourceFormat,
            'content_format' => $contentFormat,
            'truncated' => $truncated,
            'max_bytes' => (int)$maxBytes,
            'included_bytes' => strlen((string)($normalized['data'] ?? '')),
            'decompressed_bytes_seen' => $decompressedBytes,
            'json_records' => $normalized['json_records'] ?? null,
            'invalid_json_lines' => $normalized['invalid_json_lines'] ?? null,
            'dropped_partial_lines' => $normalized['dropped_partial_lines'] ?? null,
            'redacted' => true,
            'readable' => true,
        ],
    ];
}

function installCenterReadRedactedFile($path, $maxBytes = 90000) {
    $payload = installCenterReadDiagnosticFile($path, basename((string)$path), $maxBytes);
    return (string)($payload['data'] ?? '');
}

function installCenterDiagnosticReadLimit($id) {
    if (str_starts_with($id, 'log:')) return 90000;
    if (str_starts_with($id, 'ramdisk:')) return 250000;
    return 90000;
}

function installCenterDiagnosticGeneratedPayload($id, $archiveName, $data, $contentFormat = 'json') {
    return [
        'name' => $archiveName,
        'data' => (string)$data,
        'meta' => [
            'id' => $id,
            'archive_name' => $archiveName,
            'source_path' => null,
            'source_compressed' => false,
            'source_format' => $contentFormat,
            'content_format' => $contentFormat,
            'truncated' => false,
            'included_bytes' => strlen((string)$data),
            'redacted' => true,
            'readable' => true,
            'generated' => true,
        ],
    ];
}

function installCenterDiagnosticIncidentContext($context = []) {
    $now = time();
    $incidentTs = is_numeric($context['incident_ts'] ?? null) ? (int)$context['incident_ts'] : $now;
    if ($incidentTs < $now - 31 * 86400 || $incidentTs > $now + 86400) $incidentTs = $now;
    $beforeS = max(60, min(6 * 3600, (int)($context['incident_before_s'] ?? 1800)));
    $afterS = max(60, min(2 * 3600, (int)($context['incident_after_s'] ?? 600)));
    return [
        'incident_ts' => $incidentTs,
        'start_ts' => $incidentTs - $beforeS,
        'end_ts' => $incidentTs + $afterS,
        'before_s' => $beforeS,
        'after_s' => $afterS,
    ];
}

function installCenterDiagnosticRowTs($row) {
    if (!is_array($row)) return 0.0;
    foreach (['ts', '_ts', 'timestamp'] as $key) {
        if (!isset($row[$key]) || !is_numeric($row[$key])) continue;
        $ts = (float)$row[$key];
        if ($ts > 10000000000) $ts /= 1000.0;
        if ($ts > 0) return $ts;
    }
    foreach (['time', 'created_at', 'iso_ts'] as $key) {
        if (empty($row[$key]) || !is_string($row[$key])) continue;
        $ts = strtotime($row[$key]);
        if ($ts !== false) return (float)$ts;
    }
    return 0.0;
}

function installCenterDiagnosticTimelineFiles($prefix, $startTs, $endTs) {
    $files = array_merge(
        glob('/var/www/html/logs/' . $prefix . '*.jsonl') ?: [],
        glob('/var/www/html/logs/' . $prefix . '*.jsonl.gz') ?: []
    );
    $startDay = date('Ymd', (int)$startTs);
    $endDay = date('Ymd', (int)$endTs);
    return array_values(array_filter($files, function($path) use ($startDay, $endDay) {
        if (!preg_match('/_(\d{8})\.jsonl(?:\.gz)?$/', basename((string)$path), $match)) return true;
        return $match[1] >= $startDay && $match[1] <= $endDay;
    }));
}

function installCenterDiagnosticCompactTimelineRow($source, $row) {
    $keys = ['decision', 'inputs', 'curve', 'wallbox', 'wallboxes', 'storage_context', 'transition', 'r5', 'command', 'reaction', 'current', 'context', 'heatpump'];
    $event = [];
    foreach ($keys as $key) {
        if (array_key_exists($key, $row)) $event[$key] = $row[$key];
    }
    if (!$event) $event = $row;
    return installCenterRedactConfigValue('', [
        'source' => (string)$source,
        'event' => $event,
    ]);
}

function installCenterDiagnosticReadTimelineJsonl($path, $source, $startTs, $endTs, &$events, $maxEvents = 2500) {
    if (!is_readable($path) || count($events) >= $maxEvents) return;
    $isGz = str_ends_with(strtolower((string)$path), '.gz');
    if ($isGz && !function_exists('gzopen')) return;
    $handle = $isGz ? @gzopen($path, 'rb') : @fopen($path, 'rb');
    if (!$handle) return;
    while (($isGz ? !gzeof($handle) : !feof($handle)) && count($events) < $maxEvents) {
        $line = $isGz ? @gzgets($handle) : @fgets($handle);
        if (!is_string($line) || trim($line) === '') continue;
        $row = json_decode($line, true);
        if (!is_array($row)) continue;
        $ts = installCenterDiagnosticRowTs($row);
        if ($ts < $startTs || $ts > $endTs) continue;
        $compact = installCenterDiagnosticCompactTimelineRow($source, $row);
        $compact['ts'] = round($ts, 3);
        $compact['time'] = date('c', (int)$ts);
        $events[] = $compact;
    }
    $isGz ? @gzclose($handle) : @fclose($handle);
}

function installCenterDiagnosticIncidentTimeline($context = []) {
    $window = installCenterDiagnosticIncidentContext($context);
    $events = [];
    $sources = [
        'storage_decision_history_' => 'storage_decision',
        'wallbox_decision_history_' => 'wallbox_decision',
        'energy_decision_history_' => 'energy_decision',
        'ems_reaction_history_' => 'ems_reaction',
    ];
    foreach ($sources as $prefix => $source) {
        foreach (installCenterDiagnosticTimelineFiles($prefix, $window['start_ts'], $window['end_ts']) as $path) {
            installCenterDiagnosticReadTimelineJsonl($path, $source, $window['start_ts'], $window['end_ts'], $events);
        }
    }
    foreach (['/var/www/html/logs/wallbox_command_audit.log.1', '/var/www/html/logs/wallbox_command_audit.log'] as $path) {
        installCenterDiagnosticReadTimelineJsonl($path, 'wallbox_command', $window['start_ts'], $window['end_ts'], $events);
    }
    foreach (installCenterReadGlitchRows(48) as $item) {
        $ts = (float)($item['ts'] ?? 0.0);
        if ($ts < $window['start_ts'] || $ts > $window['end_ts']) continue;
        $events[] = [
            'ts' => round($ts, 3),
            'time' => date('c', (int)$ts),
            'source' => 'live_plausibility',
            'event' => installCenterGlitchCompactEvent($item),
        ];
    }
    usort($events, fn($a, $b) => (($a['ts'] ?? 0) <=> ($b['ts'] ?? 0)));
    if (count($events) > 1500) {
        usort($events, fn($a, $b) => abs(($a['ts'] ?? 0) - $window['incident_ts']) <=> abs(($b['ts'] ?? 0) - $window['incident_ts']));
        $events = array_slice($events, 0, 1500);
        usort($events, fn($a, $b) => (($a['ts'] ?? 0) <=> ($b['ts'] ?? 0)));
    }
    array_unshift($events, [
        'ts' => $window['incident_ts'],
        'time' => date('c', $window['incident_ts']),
        'source' => 'diagnostic_bundle',
        'event' => [
            'schema_version' => 'e3dc_incident_timeline_v1',
            'window_start' => date('c', $window['start_ts']),
            'window_end' => date('c', $window['end_ts']),
            'event_count' => count($events),
        ],
    ]);
    return implode("\n", array_map(
        fn($row) => json_encode($row, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES),
        $events
    )) . "\n";
}

function installCenterDiagnosticItemPayloadRaw($id, $context = []) {
    $archiveName = installCenterDiagnosticArchiveName($id);
    if ($archiveName === null) return null;
    if ($id === 'config:redacted') {
        return installCenterDiagnosticGeneratedPayload(
            $id,
            $archiveName,
            json_encode(installCenterRedactedConfig(), JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) . "\n"
        );
    }
    if ($id === 'status:installer') {
        return installCenterDiagnosticGeneratedPayload(
            $id,
            $archiveName,
            json_encode(installCenterRedactConfigValue('', runInstallerAction('installer_status')), JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) . "\n"
        );
    }
    if ($id === 'status:diagnosis') {
        return installCenterDiagnosticGeneratedPayload(
            $id,
            $archiveName,
            json_encode(installCenterRedactConfigValue('', runInstallerAction('run_diagnosis')), JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) . "\n"
        );
    }
    if ($id === 'status:job') {
        return installCenterDiagnosticGeneratedPayload(
            $id,
            $archiveName,
            json_encode(installCenterRedactConfigValue('', runInstallerAction('job_status')), JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) . "\n"
        );
    }
    if ($id === 'status:ml_docker') {
        return installCenterDiagnosticGeneratedPayload(
            $id,
            $archiveName,
            json_encode(installCenterRedactConfigValue('', installCenterMlDockerStatus()), JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) . "\n"
        );
    }
    if ($id === 'status:power_decision') {
        return installCenterDiagnosticGeneratedPayload(
            $id,
            $archiveName,
            json_encode(installCenterRedactConfigValue('', installCenterPowerDecisionDiagnosticsStatus()), JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) . "\n"
        );
    }
    if ($id === 'status:glitch_situations') {
        return installCenterDiagnosticGeneratedPayload(
            $id,
            $archiveName,
            json_encode(installCenterRedactConfigValue('', installCenterGlitchSituationDiagnosticsStatus()), JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) . "\n"
        );
    }
    if ($id === 'status:incident_timeline') {
        return installCenterDiagnosticGeneratedPayload(
            $id,
            $archiveName,
            installCenterDiagnosticIncidentTimeline($context),
            'jsonl'
        );
    }
    if ($id === 'status:direct_marketing') {
        return installCenterDiagnosticGeneratedPayload(
            $id,
            $archiveName,
            json_encode(installCenterRedactConfigValue('', installCenterDirectMarketingDiagnosticsStatus()), JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) . "\n"
        );
    }
    if (str_starts_with($id, 'log:')) {
        $payload = installCenterReadDiagnosticFile('/var/www/html/logs/' . basename(substr($id, 4)), $archiveName, installCenterDiagnosticReadLimit($id));
        $payload['meta']['id'] = $id;
        return $payload;
    }
    if (str_starts_with($id, 'ramdisk:')) {
        $payload = installCenterReadDiagnosticFile('/var/www/html/ramdisk/' . basename(substr($id, 8)), $archiveName, installCenterDiagnosticReadLimit($id));
        $payload['meta']['id'] = $id;
        return $payload;
    }
    return null;
}

function installCenterFinalizeDiagnosticPayload($payload) {
    if (!is_array($payload) || !array_key_exists('data', $payload)) return $payload;
    $data = (string)$payload['data'];
    $meta = is_array($payload['meta'] ?? null) ? $payload['meta'] : [];
    $capturedAt = microtime(true);
    $sourcePath = (string)($meta['source_path'] ?? '');
    $sourceMtime = ($sourcePath !== '' && is_file($sourcePath)) ? @filemtime($sourcePath) : false;
    $meta['capture_at'] = date('c', (int)$capturedAt);
    $meta['capture_ts'] = round($capturedAt, 6);
    $meta['included_bytes'] = strlen($data);
    $meta['sha256'] = hash('sha256', $data);
    $meta['source_mtime'] = $sourceMtime !== false ? date('c', (int)$sourceMtime) : null;
    $meta['source_age_s'] = $sourceMtime !== false ? round(max(0.0, $capturedAt - (float)$sourceMtime), 3) : null;
    $payload['meta'] = $meta;
    return $payload;
}

function installCenterDiagnosticItemPayload($id, $context = []) {
    return installCenterFinalizeDiagnosticPayload(installCenterDiagnosticItemPayloadRaw($id, $context));
}

function installCenterDiagnosticItemText($id) {
    $payload = installCenterDiagnosticItemPayload($id);
    return is_array($payload) ? ($payload['data'] ?? null) : null;
}

function installCenterDiagnosticArchiveName($id) {
    if ($id === 'config:redacted') return 'config/e3dc_v4_redacted.json';
    if ($id === 'status:installer') return 'status/installer_status.json';
    if ($id === 'status:diagnosis') return 'status/module_diagnosis.json';
    if ($id === 'status:job') return 'status/job_status.json';
    if ($id === 'status:ml_docker') return 'status/ml_docker_status.json';
    if ($id === 'status:power_decision') return 'status/power_decision_diagnosis.json';
    if ($id === 'status:glitch_situations') return 'status/glitch_situation_summary.json';
    if ($id === 'status:incident_timeline') return 'status/incident_timeline.jsonl';
    if ($id === 'status:direct_marketing') return 'status/direct_marketing_diagnosis.json';
    if (str_starts_with($id, 'log:')) return 'logs/' . basename(substr($id, 4));
    if (str_starts_with($id, 'ramdisk:')) return 'ramdisk/' . basename(substr($id, 8));
    return null;
}

function installCenterDosTimeDate() {
    $now = getdate();
    $year = max(1980, (int)$now['year']);
    $dosTime = ((int)$now['hours'] << 11) | ((int)$now['minutes'] << 5) | (int)floor((int)$now['seconds'] / 2);
    $dosDate = (($year - 1980) << 9) | ((int)$now['mon'] << 5) | (int)$now['mday'];
    return [$dosTime, $dosDate];
}

function installCenterZipEntryPayload($data, $level = 9) {
    $data = (string)$data;
    $method = 0;
    $payload = $data;
    if (function_exists('gzdeflate')) {
        $compressed = @gzdeflate($data, max(0, min(9, (int)$level)));
        if (is_string($compressed) && strlen($compressed) < strlen($data)) {
            $method = 8;
            $payload = $compressed;
        }
    }
    return [$method, $payload];
}

function installCenterZipLocalHeader($name, $data, $offset, $dosTime, $dosDate, $method = null, $payload = null) {
    $data = (string)$data;
    if ($method === null || $payload === null) {
        [$method, $payload] = installCenterZipEntryPayload($data);
    }
    $crc = crc32($data);
    $size = strlen($data);
    $compressedSize = strlen($payload);
    $nameLen = strlen($name);
    $local = pack('V', 0x04034b50)
        . pack('v', 20)
        . pack('v', 0)
        . pack('v', (int)$method)
        . pack('v', $dosTime)
        . pack('v', $dosDate)
        . pack('V', $crc)
        . pack('V', $compressedSize)
        . pack('V', $size)
        . pack('v', $nameLen)
        . pack('v', 0)
        . $name
        . $payload;
    $central = pack('V', 0x02014b50)
        . pack('v', 20)
        . pack('v', 20)
        . pack('v', 0)
        . pack('v', (int)$method)
        . pack('v', $dosTime)
        . pack('v', $dosDate)
        . pack('V', $crc)
        . pack('V', $compressedSize)
        . pack('V', $size)
        . pack('v', $nameLen)
        . pack('v', 0)
        . pack('v', 0)
        . pack('v', 0)
        . pack('v', 0)
        . pack('V', 0)
        . pack('V', $offset)
        . $name;
    return [$local, $central];
}

function installCenterBuildZipBytes($entries) {
    [$dosTime, $dosDate] = installCenterDosTimeDate();
    $body = '';
    $central = '';
    $offset = 0;
    $count = 0;
    foreach ($entries as $entry) {
        $name = str_replace('\\', '/', trim((string)($entry['name'] ?? ''), '/'));
        $data = (string)($entry['data'] ?? '');
        if ($name === '') continue;
        [$method, $payload] = installCenterZipEntryPayload($data, 9);
        [$local, $dir] = installCenterZipLocalHeader($name, $data, $offset, $dosTime, $dosDate, $method, $payload);
        $body .= $local;
        $central .= $dir;
        $offset += strlen($local);
        $count++;
    }
    $centralSize = strlen($central);
    $centralOffset = strlen($body);
    $end = pack('V', 0x06054b50)
        . pack('v', 0)
        . pack('v', 0)
        . pack('v', $count)
        . pack('v', $count)
        . pack('V', $centralSize)
        . pack('V', $centralOffset)
        . pack('v', 0);
    return $body . $central . $end;
}

function installCenterZipArchiveAddEntry($zip, $entry) {
    $name = str_replace('\\', '/', trim((string)($entry['name'] ?? ''), '/'));
    if ($name === '' || !is_object($zip)) return false;
    $data = (string)($entry['data'] ?? '');
    if (!$zip->addFromString($name, $data)) return false;
    if (!method_exists($zip, 'setCompressionName')) return true;
    $meta = is_array($entry['meta'] ?? null) ? $entry['meta'] : [];
    $alreadyCompressed = str_ends_with(strtolower($name), '.gz') || !empty($meta['raw_machine_file']);
    if ($alreadyCompressed && defined('ZipArchive::CM_STORE')) {
        @$zip->setCompressionName($name, ZipArchive::CM_STORE);
    } elseif (defined('ZipArchive::CM_DEFLATE')) {
        @$zip->setCompressionName($name, ZipArchive::CM_DEFLATE, 9);
    }
    return true;
}

function installCenterStreamDiagnosticBundle($selectedIds, $options = []) {
    $captureStarted = microtime(true);
    $incident = installCenterDiagnosticIncidentContext($options);
    $manifest = installCenterDiagnosticCandidates();
    $allowed = array_column($manifest['items'], 'id');
    if (!is_array($selectedIds) || !$selectedIds) {
        $selectedIds = array_values(array_map(fn($item) => $item['id'], array_filter($manifest['items'], fn($item) => !empty($item['default']))));
    }
    $selectedIds = array_values(array_intersect(array_unique(array_map('strval', $selectedIds)), $allowed));
    if (!$selectedIds) {
        header('Content-Type: application/json; charset=utf-8');
        echo json_encode(['success' => false, 'error' => 'Keine Diagnose-Dateien ausgewählt'], JSON_UNESCAPED_UNICODE);
        exit;
    }
    $entries = [];
    $fileName = 'e3dc_diagnose_' . date('Ymd_His') . '.zip';
    $zipPath = null;
    $zip = null;
    if (class_exists('ZipArchive')) {
        $zipPath = tempnam(sys_get_temp_dir(), 'e3dc_diag_');
        if ($zipPath === false) {
            header('Content-Type: application/json; charset=utf-8');
            echo json_encode(['success' => false, 'error' => 'Temporäre Zip-Datei konnte nicht angelegt werden'], JSON_UNESCAPED_UNICODE);
            exit;
        }
        $zip = new ZipArchive();
        if ($zip->open($zipPath, ZipArchive::OVERWRITE) !== true) {
            @unlink($zipPath);
            header('Content-Type: application/json; charset=utf-8');
            echo json_encode(['success' => false, 'error' => 'Zip-Datei konnte nicht geöffnet werden'], JSON_UNESCAPED_UNICODE);
            exit;
        }
    }
    $appendEntry = function($entry) use (&$entries, $zip) {
        if ($zip instanceof ZipArchive) {
            installCenterZipArchiveAddEntry($zip, $entry);
            return;
        }
        $entries[] = $entry;
    };
    $versionMeta = installCenterVersionMetadata();
    $versionText = trim((string)($versionMeta['version'] ?? ''));
    $versionLine = $versionText !== '' ? $versionText : 'unbekannt';
    $gitMeta = is_array($versionMeta['git'] ?? null) ? $versionMeta['git'] : [];
    $commitLine = !empty($gitMeta['commit']) ? (string)$gitMeta['commit'] : 'unbekannt';
    $dirtyLine = array_key_exists('dirty', $gitMeta)
        ? (($gitMeta['dirty'] === null) ? 'unbekannt' : ($gitMeta['dirty'] ? 'ja' : 'nein'))
        : 'unbekannt';
    $readme = "E3DC-Control Diagnosepaket\n"
        . "Erstellt: " . date('c') . "\n\n"
        . "Version: " . $versionLine . "\n"
        . "Git-Commit: " . $commitLine . "\n"
        . "Git-Worktree geändert: " . $dirtyLine . "\n\n"
        . "Datenschutz: Passwörter, Tokens, E-Mail-Adressen, Chat-IDs und Standortwerte wurden automatisch maskiert.\n"
        . "Fahrzeug-, Netzwerk- und MQTT-Identitäten wurden konsistent pseudonymisiert.\n"
        . "Bitte das Paket vor dem Versenden einmal öffnen und prüfen.\n"
        . "Maschinenlesbarkeit: JSON/JSONL-Dateien enthalten keine Kommentar- oder Hinweiszeilen.\n"
        . "Maschinenhistorien werden immer geparst, gekürzt und strukturiert redigiert.\n"
        . "Gekürzte JSONL-Logs enden auf .tail.jsonl; Details stehen in status/diagnostic_bundle_manifest.json.\n";
    $readmePayload = installCenterFinalizeDiagnosticPayload(installCenterDiagnosticGeneratedPayload(
        'README_DATENSCHUTZ.txt', 'README_DATENSCHUTZ.txt', $readme, 'text'
    ));
    $versionPayload = installCenterFinalizeDiagnosticPayload(installCenterDiagnosticGeneratedPayload(
        'status/version',
        'status/version.json',
        json_encode($versionMeta, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) . "\n",
        'json'
    ));
    $appendEntry(['name' => $readmePayload['name'], 'data' => $readmePayload['data'], 'meta' => $readmePayload['meta']]);
    $appendEntry(['name' => $versionPayload['name'], 'data' => $versionPayload['data'], 'meta' => $versionPayload['meta']]);
    $fileManifest = [$readmePayload['meta'], $versionPayload['meta']];
    foreach ($selectedIds as $id) {
        $payload = installCenterDiagnosticItemPayload($id, $incident);
        if (!is_array($payload) || empty($payload['name']) || !array_key_exists('data', $payload)) continue;
        $appendEntry(['name' => (string)$payload['name'], 'data' => (string)$payload['data'], 'meta' => $payload['meta'] ?? []]);
        if (isset($payload['meta']) && is_array($payload['meta'])) {
            $fileManifest[] = $payload['meta'];
        }
    }
    $captureFinished = microtime(true);
    $captureTimes = array_values(array_filter(array_map(fn($meta) => $meta['capture_ts'] ?? null, $fileManifest), 'is_numeric'));
    $payloadBytes = array_sum(array_map(fn($meta) => (int)($meta['included_bytes'] ?? 0), $fileManifest));
    $bundleManifest = [
        'schema_version' => 'e3dc_diagnose_bundle_manifest_v2',
        'created_at' => date('c'),
        'capture_started_at' => date('c', (int)$captureStarted),
        'capture_finished_at' => date('c', (int)$captureFinished),
        'capture_duration_ms' => round(($captureFinished - $captureStarted) * 1000.0, 1),
        'max_snapshot_skew_ms' => count($captureTimes) > 1 ? round((max($captureTimes) - min($captureTimes)) * 1000.0, 1) : 0.0,
        'payload_uncompressed_bytes' => $payloadBytes,
        'version' => $versionLine,
        'git_commit' => $commitLine,
        'machine_readable' => true,
        'incident_window' => [
            'incident_at' => date('c', $incident['incident_ts']),
            'start_at' => date('c', $incident['start_ts']),
            'end_at' => date('c', $incident['end_ts']),
            'before_s' => $incident['before_s'],
            'after_s' => $incident['after_s'],
        ],
        'selected_ids' => $selectedIds,
        'files' => $fileManifest,
        'notes' => [
            'jsonl_entries_have_no_human_prefix_lines' => true,
            'tail_jsonl_suffix_marks_truncated_jsonl' => true,
            'source_compressed_means_original_file_was_gzip' => true,
            'raw_machine_files_included' => false,
            'machine_histories_are_structurally_redacted' => true,
        ],
        'privacy_note' => 'Dateiinhalte sind redigiert oder pseudonymisiert; das Manifest enthält Formate, Alter, Prüfsummen und Aufnahmezeiten, aber keine Zugangsdaten.',
    ];
    $bundleManifestPayload = installCenterFinalizeDiagnosticPayload(installCenterDiagnosticGeneratedPayload(
        'status/diagnostic_bundle_manifest',
        'status/diagnostic_bundle_manifest.json',
        json_encode($bundleManifest, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) . "\n",
        'json'
    ));
    $appendEntry(['name' => $bundleManifestPayload['name'], 'data' => $bundleManifestPayload['data'], 'meta' => $bundleManifestPayload['meta']]);
    if (!$zip instanceof ZipArchive) {
        $zipBytes = installCenterBuildZipBytes($entries);
        header('Content-Type: application/zip');
        header('Content-Disposition: attachment; filename="' . $fileName . '"');
        header('Content-Length: ' . strlen($zipBytes));
        echo $zipBytes;
        exit;
    }
    $zip->close();
    header('Content-Type: application/zip');
    header('Content-Disposition: attachment; filename="' . $fileName . '"');
    header('Content-Length: ' . filesize($zipPath));
    readfile($zipPath);
    @unlink($zipPath);
    exit;
}

if (isset($_GET['action']) && $_GET['action'] === 'module_config') {
    requireWebAuth(true);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(installCenterBuildModuleConfigPayload($_GET['module'] ?? ''), JSON_UNESCAPED_UNICODE);
    exit;
}

if (isset($_GET['action']) && $_GET['action'] === 'save_module_config') {
    requireWebAuth(true);
    header('Content-Type: application/json; charset=utf-8');
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        echo json_encode(['success' => false, 'error' => 'Config-Speichern nur per POST erlaubt'], JSON_UNESCAPED_UNICODE);
        exit;
    }
    if (!validateInstallCenterCsrf()) {
        echo json_encode(['success' => false, 'error' => 'Sicherheits-Token ungültig. Bitte Seite neu laden.'], JSON_UNESCAPED_UNICODE);
        exit;
    }
    echo json_encode(installCenterSaveModuleConfig($_POST['module'] ?? '', $_POST['values'] ?? []), JSON_UNESCAPED_UNICODE);
    exit;
}

if (isset($_GET['action']) && $_GET['action'] === 'diagnostic_manifest') {
    requireWebAuth(true);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(installCenterDiagnosticCandidates(), JSON_UNESCAPED_UNICODE);
    exit;
}

if (isset($_GET['action']) && $_GET['action'] === 'diagnostic_bundle') {
    requireWebAuth(true);
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        header('Content-Type: application/json; charset=utf-8');
        echo json_encode(['success' => false, 'error' => 'Diagnosepaket nur per POST erlaubt'], JSON_UNESCAPED_UNICODE);
        exit;
    }
    if (!validateInstallCenterCsrf()) {
        header('Content-Type: application/json; charset=utf-8');
        echo json_encode(['success' => false, 'error' => 'Sicherheits-Token ungültig. Bitte Seite neu laden.'], JSON_UNESCAPED_UNICODE);
        exit;
    }
    $incidentRaw = $_POST['incident_ts'] ?? null;
    $incidentTs = is_numeric($incidentRaw) ? (int)$incidentRaw : (is_string($incidentRaw) ? strtotime($incidentRaw) : false);
    installCenterStreamDiagnosticBundle($_POST['items'] ?? [], [
        'incident_ts' => $incidentTs !== false ? $incidentTs : time(),
        'incident_before_s' => (int)($_POST['incident_before_s'] ?? 1800),
        'incident_after_s' => (int)($_POST['incident_after_s'] ?? 600),
    ]);
}

if (isset($_GET['action']) && $_GET['action'] === 'catalog') {
    requireWebAuth(true);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(runInstallerAction('catalog'), JSON_UNESCAPED_UNICODE);
    exit;
}

if (isset($_GET['action']) && $_GET['action'] === 'installer_status') {
    requireWebAuth(true);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(runInstallerAction('installer_status'), JSON_UNESCAPED_UNICODE);
    exit;
}

if (isset($_GET['action']) && $_GET['action'] === 'job_status') {
    requireWebAuth(true);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(runInstallerAction('job_status'), JSON_UNESCAPED_UNICODE);
    exit;
}

if (isset($_GET['action']) && $_GET['action'] === 'run_job') {
    requireWebAuth(true);
    header('Content-Type: application/json; charset=utf-8');
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        echo json_encode(['success' => false, 'error' => 'Job-Start nur per POST erlaubt'], JSON_UNESCAPED_UNICODE);
        exit;
    }
    if (!validateInstallCenterCsrf()) {
        echo json_encode(['success' => false, 'error' => 'Sicherheits-Token ungültig. Bitte Seite neu laden.'], JSON_UNESCAPED_UNICODE);
        exit;
    }
    echo json_encode(runInstallerJob($_POST['job_action'] ?? '', $_POST['module'] ?? null), JSON_UNESCAPED_UNICODE);
    exit;
}

if (isset($_GET['action']) && $_GET['action'] === 'run_wrapper_job') {
    requireWebAuth(true);
    header('Content-Type: application/json; charset=utf-8');
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        echo json_encode(['success' => false, 'error' => 'Wrapper-Job-Start nur per POST erlaubt'], JSON_UNESCAPED_UNICODE);
        exit;
    }
    if (!validateInstallCenterCsrf()) {
        echo json_encode(['success' => false, 'error' => 'Sicherheits-Token ungültig. Bitte Seite neu laden.'], JSON_UNESCAPED_UNICODE);
        exit;
    }
    echo json_encode(runInstallerJob($_POST['job_action'] ?? '', $_POST['module'] ?? null, true), JSON_UNESCAPED_UNICODE);
    exit;
}

if (isset($_GET['action']) && $_GET['action'] === 'run_wrapper_write_job') {
    requireWebAuth(true);
    header('Content-Type: application/json; charset=utf-8');
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        echo json_encode(['success' => false, 'error' => 'Schreibender Wrapper-Job nur per POST erlaubt'], JSON_UNESCAPED_UNICODE);
        exit;
    }
    if (!validateInstallCenterCsrf()) {
        echo json_encode(['success' => false, 'error' => 'Sicherheits-Token ungültig. Bitte Seite neu laden.'], JSON_UNESCAPED_UNICODE);
        exit;
    }
    echo json_encode(runInstallerWriteJob($_POST['job_action'] ?? '', $_POST['module'] ?? null), JSON_UNESCAPED_UNICODE);
    exit;
}

if (isset($_GET['action']) && $_GET['action'] === 'write_readiness') {
    requireWebAuth(true);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(runInstallerAction('write_readiness'), JSON_UNESCAPED_UNICODE);
    exit;
}

if (isset($_GET['action']) && $_GET['action'] === 'write_permission_plan') {
    requireWebAuth(true);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(runInstallerAction('write_permission_plan'), JSON_UNESCAPED_UNICODE);
    exit;
}

if (isset($_GET['action']) && $_GET['action'] === 'backup_plan') {
    requireWebAuth(true);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(runInstallerAction('backup_plan', $_GET['module'] ?? null), JSON_UNESCAPED_UNICODE);
    exit;
}

if (isset($_GET['action']) && $_GET['action'] === 'diagnosis') {
    requireWebAuth(true);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(runInstallerAction('run_diagnosis', $_GET['module'] ?? null), JSON_UNESCAPED_UNICODE);
    exit;
}

if (isset($_GET['action']) && $_GET['action'] === 'dry_run') {
    requireWebAuth(true);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(runInstallerAction('dry_run', $_GET['module'] ?? null), JSON_UNESCAPED_UNICODE);
    exit;
}

if (isset($_GET['action']) && $_GET['action'] === 'install_module_dry_run') {
    requireWebAuth(true);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(runInstallerAction('install_module_dry_run', $_GET['module'] ?? null), JSON_UNESCAPED_UNICODE);
    exit;
}

if (isset($_GET['action']) && $_GET['action'] === 'install_module') {
    requireWebAuth(true);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode([
        'success' => false,
        'write_blocked' => true,
        'message' => 'Echte Modulinstallation ist in dieser WebUI-Vorstufe noch nicht freigeschaltet. Bitte zuerst den Install-Dry-Run oder Job-Test nutzen.'
    ], JSON_UNESCAPED_UNICODE);
    exit;
}

if (isset($_GET['action']) && $_GET['action'] === 'permissions_check') {
    requireWebAuth(true);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(runInstallerAction('permissions_check'), JSON_UNESCAPED_UNICODE);
    exit;
}

if (isset($_GET['action']) && $_GET['action'] === 'repair_permissions_dry_run') {
    requireWebAuth(true);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(runInstallerAction('repair_permissions_dry_run'), JSON_UNESCAPED_UNICODE);
    exit;
}

if (isset($_GET['action']) && $_GET['action'] === 'repair_runtime_permissions') {
    requireWebAuth(true);
    header('Content-Type: application/json; charset=utf-8');
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        http_response_code(405);
        echo json_encode(['success' => false, 'error' => 'Rechtereparatur nur per POST erlaubt'], JSON_UNESCAPED_UNICODE);
        exit;
    }
    if (!validateInstallCenterCsrf()) {
        http_response_code(403);
        echo json_encode(['success' => false, 'error' => 'Sicherheits-Token ungültig. Bitte Seite neu laden.'], JSON_UNESCAPED_UNICODE);
        exit;
    }
    $confirmContentDrift = isset($_POST['confirm_content_drift'])
        && (string)$_POST['confirm_content_drift'] === '1';
    $confirmationToken = $confirmContentDrift
        ? (string)($_POST['confirmation_token'] ?? '')
        : '';
    if (!$confirmContentDrift && isset($_POST['confirmation_token'])) {
        http_response_code(400);
        echo json_encode([
            'success' => false,
            'error_code' => 'confirmation_mode_invalid',
            'message' => 'Eine Dateilistenfreigabe wurde ohne bewusste Bestätigung übergeben.',
        ], JSON_UNESCAPED_UNICODE);
        exit;
    }
    echo json_encode(
        runRuntimePermissionsRepair(false, $confirmationToken),
        JSON_UNESCAPED_UNICODE
    );
    exit;
}

if (isset($_GET['action']) && $_GET['action'] === 'check_runtime_permissions_repair') {
    requireWebAuth(true);
    header('Content-Type: application/json; charset=utf-8');
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        http_response_code(405);
        echo json_encode(['success' => false, 'error' => 'Rechte-Preflight nur per POST erlaubt'], JSON_UNESCAPED_UNICODE);
        exit;
    }
    if (!validateInstallCenterCsrf()) {
        http_response_code(403);
        echo json_encode(['success' => false, 'error' => 'Sicherheits-Token ungültig. Bitte Seite neu laden.'], JSON_UNESCAPED_UNICODE);
        exit;
    }
    echo json_encode(runRuntimePermissionsRepair(true), JSON_UNESCAPED_UNICODE);
    exit;
}
?>
<!DOCTYPE html>
<html lang="de" data-bs-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>E3DC-Control Installationszentrale</title>
    <link href="assets/vendor/bootstrap/css/bootstrap.min.css" rel="stylesheet">
    <link href="assets/vendor/fontawesome/css/all.min.css" rel="stylesheet">
    <style>
        :root {
            --bg: #101112;
            --panel: #1b1d1f;
            --panel-soft: #22262a;
            --line: #343a40;
            --cyan: #00d9ff;
            --green: #18a666;
            --yellow: #ffc107;
            --red: #e63757;
        }
        body { background: var(--bg); color: #e9ecef; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
        .page-shell { max-width: 1520px; margin: 0 auto; padding: 26px 18px 40px; }
        .topbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 20px; }
        .back-btn { border: 1px solid #3a4148; color: #ced4da; text-decoration: none; padding: 7px 12px; border-radius: 6px; }
        .back-btn:hover { border-color: var(--cyan); color: #fff; }
        .security-note { border: 1px solid rgba(0,217,255,.35); background: rgba(0,217,255,.08); border-radius: 8px; padding: 12px 14px; }
        .module-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(330px, 1fr)); gap: 14px; }
        .module-card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 15px; min-height: 228px; }
        .module-card.core { border-left: 4px solid #0d6efd; }
        .module-card.consumers { border-left: 4px solid var(--green); }
        .module-card.integrations { border-left: 4px solid #9b7cff; }
        .module-card.system { border-left: 4px solid var(--yellow); }
        .module-title { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; margin-bottom: 8px; }
        .module-title h2 { font-size: 1.02rem; margin: 0; }
        .desc { color: #adb5bd; font-size: .9rem; line-height: 1.35; min-height: 40px; }
        .status-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin: 12px 0; }
        .status-pill { background: var(--panel-soft); border: 1px solid #394047; border-radius: 6px; padding: 8px; font-size: .78rem; min-height: 56px; }
        .status-pill strong { display: block; font-size: .88rem; color: #fff; }
        .module-readiness { border: 1px solid #303840; border-radius: 6px; padding: 8px; margin: 10px 0 2px; background: #111417; font-size: .84rem; }
        .module-readiness strong { display: block; margin-bottom: 2px; }
        .module-readiness.ready { border-color: rgba(24,166,102,.5); }
        .module-readiness.installed { border-color: rgba(13,110,253,.55); }
        .module-readiness.blocked { border-color: rgba(230,55,87,.6); }
        .module-readiness.docker_pending { border-color: rgba(255,193,7,.6); }
        .ok { color: #35d07f; }
        .warn { color: var(--yellow); }
        .bad { color: #ff5f78; }
        .module-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
        .primary-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
        .primary-actions .btn,
        .module-actions .btn { border-radius: 999px; font-size: .82rem; }
        .advanced-actions { margin-top: 8px; border-top: 1px solid #2d3339; padding-top: 8px; }
        .advanced-actions summary { cursor: pointer; color: #8f98a3; font-size: .78rem; user-select: none; }
        .advanced-actions .module-actions { margin-top: 8px; }
        .btn-locked { opacity: .58; cursor: not-allowed; }
        .action-hint { color: #8f98a3; font-size: .78rem; margin-top: 8px; line-height: 1.35; }
        .utility-bar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin: 16px 0 18px; }
        .installer-status { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; margin: 0 0 16px; }
        .installer-status-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
        .installer-status-badges { display: flex; gap: 8px; flex-wrap: wrap; }
        .installer-status-meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 8px; margin-top: 10px; }
        .installer-status-meta div { background: var(--panel-soft); border: 1px solid #394047; border-radius: 6px; padding: 8px; font-size: .85rem; }
        .group-heading { display: flex; align-items: center; gap: 8px; margin: 24px 0 10px; color: var(--cyan); text-transform: uppercase; font-size: .85rem; letter-spacing: .04em; }
        .small-code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; color: #9ee7ff; font-size: .78rem; word-break: break-all; }
        .log-box { background: #090a0b; border: 1px solid #30363d; border-radius: 8px; min-height: 48px; padding: 10px; color: #c9d1d9; }
        .result-title { display: flex; align-items: center; gap: 8px; font-weight: 700; margin-bottom: 8px; }
        .result-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 8px; margin: 10px 0; }
        .result-tile { background: #111417; border: 1px solid #2d3339; border-radius: 6px; padding: 8px; font-size: .86rem; }
        .result-tile strong { display: block; margin-bottom: 2px; }
        .result-list { margin: 8px 0 0; padding-left: 18px; }
        .result-list li { margin: 3px 0; }
        .raw-json { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: .78rem; white-space: pre-wrap; background: #050607; border: 1px solid #252b31; border-radius: 6px; padding: 8px; margin-top: 10px; max-height: 320px; overflow: auto; }
        .rule-calm-controls { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; align-items: end; margin-top: 12px; }
        .rule-calm-control label { display: block; color: var(--muted); font-size: .78rem; margin-bottom: 4px; }
        .rule-calm-service-row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; min-height: 38px; }
        .rule-calm-service-row label { display: inline-flex; align-items: center; gap: 5px; margin: 0; color: var(--text); font-size: .84rem; }
        .rule-calm-upload { display: none; margin-top: 8px; }
        .rule-calm-upload.active { display: block; }
        .rule-calm-table-wrap { overflow-x: auto; }
        .rule-calm-table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: .83rem; }
        .rule-calm-table th, .rule-calm-table td { border-bottom: 1px solid #2d3339; padding: 6px 5px; vertical-align: top; }
        .rule-calm-table th { color: var(--muted); font-weight: 600; }
        .rule-calm-timeline { display: grid; gap: 6px; margin-top: 8px; }
        .rule-calm-event { display: grid; grid-template-columns: minmax(72px, .45fr) minmax(112px, .8fr) minmax(130px, max-content) minmax(0, 1.8fr); gap: 8px; align-items: start; background: #0c0f12; border: 1px solid #2d3339; border-radius: 6px; padding: 7px 8px; font-size: .82rem; }
        .rule-calm-event > div { min-width: 0; }
        .rule-calm-event.alert { border-color: rgba(255,193,7,.58); background: rgba(255,193,7,.08); }
        .rule-calm-time { white-space: nowrap; }
        .rule-calm-lane { color: #9ee7ff; font-weight: 700; overflow-wrap: anywhere; }
        .rule-calm-action { display: inline-flex; justify-content: center; max-width: 100%; min-width: 56px; border-radius: 999px; border: 1px solid #3c444c; padding: 1px 7px; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: .75rem; line-height: 1.2; text-align: center; white-space: normal; overflow-wrap: anywhere; }
        .rule-calm-action.start, .rule-calm-action.chrg, .rule-calm-action.boost, .rule-calm-action.run, .rule-calm-action.allow, .rule-calm-action.command { color: #35d07f; border-color: rgba(53,208,127,.45); }
        .rule-calm-action.stop, .rule-calm-action.disch, .rule-calm-action.off, .rule-calm-action.block { color: #ffb454; border-color: rgba(255,180,84,.45); }
        .rule-calm-action.obs_run, .rule-calm-action.obs_off, .rule-calm-action.observe, .rule-calm-action.noop { color: #9aa8b5; border-color: rgba(154,168,181,.45); }
        .rule-calm-action.idle, .rule-calm-action.auto, .rule-calm-action.auto_guard { color: #9ee7ff; border-color: rgba(158,231,255,.45); }
        .rule-calm-detail { min-width: 0; overflow-wrap: anywhere; }
        @media (max-width: 760px) {
            .rule-calm-event { grid-template-columns: 1fr; }
            .rule-calm-controls { grid-template-columns: 1fr; }
            #ruleCalmAnalysisBox .result-grid { grid-template-columns: 1fr; }
            .rule-calm-table { min-width: 0; }
            .rule-calm-table thead { display: none; }
            .rule-calm-table tbody, .rule-calm-table tr, .rule-calm-table td { display: block; width: 100%; }
            .rule-calm-table tr { border: 1px solid #2d3339; border-radius: 6px; padding: 5px 8px; }
            .rule-calm-table td { display: grid; grid-template-columns: 92px minmax(0, 1fr); gap: 8px; border: 0; padding: 4px 0; overflow-wrap: anywhere; }
            .rule-calm-table td::before { content: attr(data-label); color: var(--muted); font-weight: 600; }
        }
        .skeleton { opacity: .65; }
        .job-modal-backdrop { position: fixed; inset: 0; z-index: 1050; background: rgba(0,0,0,.68); display: flex; align-items: center; justify-content: center; padding: 18px; }
        .job-modal-panel { width: min(760px, 100%); max-height: min(84vh, 760px); overflow: auto; background: #15181b; border: 1px solid #3b444d; border-radius: 8px; box-shadow: 0 20px 70px rgba(0,0,0,.5); }
        .job-modal-panel.wide { width: min(1080px, 100%); }
        .job-modal-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 15px 16px; border-bottom: 1px solid #303840; }
        .job-modal-body { padding: 15px 16px; }
        .job-modal-actions { display: flex; justify-content: flex-end; gap: 8px; padding: 12px 16px 16px; border-top: 1px solid #303840; }
        .config-field-list { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
        .config-field-chip { border: 1px solid #46505a; background: #20262b; border-radius: 999px; padding: 5px 9px; font-size: .82rem; color: #d7e2ea; }
        .config-field-chip.missing { border-color: #f59f00; color: #ffd43b; background: rgba(245,159,0,.12); }
        .config-field-chip.required { border-color: #0dcaf0; color: #8be9ff; background: rgba(13,202,240,.1); }
        .config-field-chip.focus { border-color: #6c757d; color: #c8d0d8; }
        .config-edit-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 10px; margin-top: 12px; }
        .config-edit-field { background: #111417; border: 1px solid #303840; border-radius: 8px; padding: 9px; }
        .config-edit-field label { display: block; font-size: .8rem; color: #dce7ef; font-weight: 700; margin-bottom: 5px; }
        .config-edit-field .form-control,
        .config-edit-field .form-select { background-color: #252b31; border-color: #48525c; color: #fff; font-size: .88rem; }
        .config-edit-help { color: #8f98a3; font-size: .76rem; line-height: 1.3; margin-top: 5px; }
        .config-save-state { min-height: 24px; margin-top: 10px; font-size: .84rem; }
        .privacy-box { border: 1px solid rgba(13,202,240,.35); background: rgba(13,202,240,.08); border-radius: 8px; padding: 10px 12px; }
        .diagnostic-preset-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px; margin-top: 12px; }
        .diagnostic-preset-button { text-align: left; background: #111417; border: 1px solid #303840; color: #dce7ef; border-radius: 8px; padding: 10px; min-height: 102px; width: 100%; }
        .diagnostic-preset-button:hover,
        .diagnostic-preset-button.active { border-color: #0dcaf0; background: rgba(13,202,240,.1); }
        .diagnostic-preset-title { display: flex; align-items: center; gap: 7px; font-weight: 800; color: #f4fbff; }
        .diagnostic-preset-desc { color: #aeb8c2; font-size: .78rem; line-height: 1.32; margin-top: 5px; }
        .diagnostic-preset-meta { color: #8f98a3; font-size: .74rem; margin-top: 7px; }
        .diagnostic-file-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 8px; margin-top: 12px; }
        .diagnostic-file-item { background: #111417; border: 1px solid #303840; border-radius: 8px; padding: 9px; }
        .diagnostic-file-item label { display: flex; gap: 8px; align-items: flex-start; cursor: pointer; }
        .diagnostic-file-item input { margin-top: 3px; }
        .diagnostic-file-meta { color: #8f98a3; font-size: .75rem; line-height: 1.3; margin-top: 4px; }
        .job-progress-box { background: #101315; border: 1px solid #2c343b; border-radius: 8px; padding: 10px 12px; }
        .d-none { display: none !important; }
    </style>
</head>
<body>
<main class="page-shell">
    <div class="topbar">
        <div>
            <a class="back-btn" href="<?= htmlspecialchars(installCenterDashboardReturnUrl(), ENT_QUOTES, 'UTF-8') ?>"><i class="fas fa-arrow-left me-1"></i> Zurück zum Dashboard</a>
            <h1 class="mt-3 mb-1 fw-bold"><i class="fas fa-screwdriver-wrench text-info me-2"></i>Installationszentrale</h1>
            <div class="text-secondary">Module installieren, Dienste prüfen und optionale Verbraucher sauber aktivieren oder deaktivieren.</div>
        </div>
        <button class="btn btn-outline-info rounded-pill" onclick="loadInstallCenter()"><i class="fas fa-rotate me-1"></i> Aktualisieren</button>
    </div>

    <div class="security-note mb-3">
        <div class="fw-bold"><i class="fas fa-shield-halved me-2"></i>Sicherheitsmodell</div>
        <div class="small mt-1">
            Diese Seite nutzt nur den zentralen Modul-Katalog und die erlaubte Service-Steuerung.
            Freie Shell-Befehle, beliebige Dienstnamen und der alte C++-Dienst <code>e3dc.service</code> sind hier nicht vorgesehen.
        </div>
    </div>

    <div class="alert alert-warning border-warning bg-warning bg-opacity-10 text-warning small">
        <i class="fas fa-triangle-exclamation me-1"></i>
        Sicherer Installationsmodus: Dienst-Start/Stop läuft nur für erlaubte Dienste.
        „Nur Rechte prüfen“ ist read-only. „Rechte reparieren“ korrigiert nur die Metadaten bekannter Pfade – ohne Backup,
        Update oder Dienstneustart. „System reparieren“ bleibt getrennt und startet den vollständigen Stable-Abgleich.
        Nutzerbeschreibbarer Installer-Code erhält keine sudo-Freigabe.
        Core-Dienste bleiben gegen Deinstallation geschützt.
    </div>

    <div class="utility-bar">
        <button class="btn btn-outline-info rounded-pill" onclick="runGlobalAction('permissions_check')">
            <i class="fas fa-shield-halved me-1"></i> Nur Rechte prüfen (keine Änderung)
        </button>
        <button class="btn btn-outline-warning rounded-pill" onclick="runPermissionRepairUpdate()">
            <i class="fas fa-tools me-1"></i> System reparieren (Backup + Stable-Abgleich)
        </button>
        <button class="btn btn-outline-secondary rounded-pill" onclick="runGlobalAction('write_readiness')">
            <i class="fas fa-user-shield me-1"></i> Freigabe prüfen
        </button>
        <button class="btn btn-outline-secondary rounded-pill" onclick="runGlobalAction('write_permission_plan')">
            <i class="fas fa-file-shield me-1"></i> Freigabe-Plan
        </button>
        <button class="btn btn-outline-secondary rounded-pill" onclick="runGlobalJob('write_permission_plan')">
            <i class="fas fa-clipboard-list me-1"></i> Freigabe-Job
        </button>
        <button class="btn btn-outline-secondary rounded-pill" disabled title="Privilegierte Installer-Webjobs sind deaktiviert">
            <i class="fas fa-lock me-1"></i> Wrapper-Webzugang gesperrt
        </button>
        <button class="btn btn-outline-info rounded-pill" onclick="runGlobalAction('backup_plan')">
            <i class="fas fa-box-archive me-1"></i> Backup-Plan
        </button>
        <button class="btn btn-outline-info rounded-pill" onclick="runGlobalJob('backup_plan')">
            <i class="fas fa-clipboard-check me-1"></i> Backup-Job
        </button>
        <button class="btn btn-outline-success rounded-pill" onclick="runGlobalAction('install_module_dry_run')">
            <i class="fas fa-traffic-light me-1"></i> Module prüfen
        </button>
        <button class="btn btn-outline-warning rounded-pill" onclick="showDiagnosticBundleModal()">
            <i class="fas fa-file-zipper me-1"></i> Diagnosepaket
        </button>
        <button class="btn btn-outline-info rounded-pill" onclick="openRuleCalmAnalysis()">
            <i class="fas fa-wave-square me-1"></i> Regelruhe prüfen
        </button>
        <button class="btn btn-outline-primary rounded-pill" onclick="runGlobalAction('job_status')">
            <i class="fas fa-clipboard-list me-1"></i> Job-Status
        </button>
        <span class="text-secondary small">Prüfaktionen lesen nur. Die enge Rechtereparatur ändert ausschließlich Besitzer, Gruppe und Modus bekannter Pfade. Systemreparatur und Update bleiben getrennte Systemjobs.</span>
    </div>

    <div id="installerStatus" class="installer-status skeleton">
        <div class="text-secondary">Lade Installer-Status...</div>
    </div>

    <!-- PV-Prognosediagnose (Verschoben von der Hauptseite) -->
    <section id="pvForecastDiagnosticBox" class="installer-status mb-4" aria-labelledby="pvForecastDiagnosticTitle">
        <div class="d-flex justify-content-between align-items-center mb-2">
            <div id="pvForecastDiagnosticTitle" class="fw-bold"><i class="fas fa-chart-line text-info me-2"></i>PV-Prognosediagnose &amp; Treffergenauigkeit</div>
            <span id="pv-forecast-diagnostic-status" class="badge text-bg-secondary">Noch keine Auswertung</span>
        </div>
        <div id="pv-forecast-diagnostic-card" class="rounded border border-secondary-subtle bg-body-tertiary px-3 py-2 small">
            <div class="d-flex flex-wrap gap-2 gap-lg-3 text-body">
                <span title="Typischer absoluter Unterschied je verglichenem 15-Minuten-Fenster">Trefferabweichung: <strong id="pv-forecast-diagnostic-hit">–</strong></span>
                <span title="Positiv bedeutet im Mittel mehr, negativ weniger Ertrag als vorhergesagt">Richtungsversatz: <strong id="pv-forecast-diagnostic-direction">–</strong></span>
                <span title="Gesamtabweichung, gewichtet nach der tatsächlich erzeugten Energie">Energieabweichung: <strong id="pv-forecast-diagnostic-energy">–</strong></span>
                <span title="Anteil der archivierten Prognosefenster mit gültigem Messwert">Abdeckung: <strong id="pv-forecast-diagnostic-coverage">–</strong></span>
            </div>
            <div class="d-flex flex-wrap justify-content-between gap-2 mt-2 text-muted">
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
    </section>

    <section id="ruleCalmAnalysisBox" class="installer-status" aria-labelledby="ruleCalmAnalysisTitle">
        <div>
            <div id="ruleCalmAnalysisTitle" class="fw-bold"><i class="fas fa-wave-square text-info me-2"></i>Regelruhe-Diagnose</div>
            <div class="text-secondary small">Prüft Entscheidungsverläufe ausschließlich read-only auf Besitzer-, Contract-, Ausführungs-, Zustands- und echte Command-Wechsel sowie regelwirksame Messwertglitches.</div>
        </div>
        <div class="rule-calm-controls">
            <div class="rule-calm-control">
                <label for="ruleCalmSourceSelect">Quelle</label>
                <select id="ruleCalmSourceSelect" class="form-select form-select-sm" onchange="updateRuleCalmSourceControls()">
                    <option value="current">Aktuelle Verlaufsdaten</option>
                    <option value="upload">Diagnose-ZIP hochladen</option>
                    <option value="history">Letzte Auswertung</option>
                </select>
            </div>
            <div class="rule-calm-control">
                <label for="ruleCalmScopeSelect">Zeitraum</label>
                <select id="ruleCalmScopeSelect" class="form-select form-select-sm">
                    <option value="manager_restart" selected>Aktueller Prozess (empfohlen)</option>
                    <option value="latest">Historie: neueste Records</option>
                </select>
            </div>
            <div class="rule-calm-control">
                <label>Dienste</label>
                <div class="rule-calm-service-row">
                    <label><input type="checkbox" class="form-check-input rule-calm-service" value="storage" checked> Speicher</label>
                    <label><input type="checkbox" class="form-check-input rule-calm-service" value="wallbox"> Wallbox</label>
                    <label><input type="checkbox" class="form-check-input rule-calm-service" value="heatpump"> Wärmepumpe</label>
                    <label><input type="checkbox" class="form-check-input rule-calm-service" value="ems"> EMS</label>
                </div>
            </div>
            <div class="rule-calm-control">
                <label for="ruleCalmMinGapSelect">Musterabstand</label>
                <select id="ruleCalmMinGapSelect" class="form-select form-select-sm">
                    <option value="60">60 Sekunden</option>
                    <option value="180" selected>180 Sekunden</option>
                    <option value="300">300 Sekunden</option>
                    <option value="600">600 Sekunden</option>
                </select>
            </div>
            <div class="rule-calm-control">
                <button class="btn btn-sm btn-outline-info w-100 rounded-pill" onclick="runRuleCalmAnalysis()">
                    <i class="fas fa-magnifying-glass-chart me-1"></i>Analyse starten
                </button>
            </div>
        </div>
        <div id="ruleCalmUploadRow" class="rule-calm-upload">
            <input id="diagnoseZipInput" type="file" class="form-control form-control-sm" accept=".zip,application/zip,application/x-zip-compressed">
            <div class="text-secondary small mt-1">Maximal 30 MB. Die Datei wird nur temporär ausgewertet und danach entfernt.</div>
        </div>
        <div id="ruleCalmAnalysisResult" class="text-secondary small mt-2" aria-live="polite">Noch nicht geprüft.</div>
    </section>

    <div id="moduleRoot" class="skeleton">
        <div class="module-grid">
            <div class="module-card"><div class="text-secondary">Lade Modulkatalog...</div></div>
        </div>
    </div>

    <div class="mt-4">
        <div class="group-heading"><i class="fas fa-terminal"></i> Rückmeldung</div>
        <div id="actionLog" class="log-box">Bereit.</div>
    </div>
</main>

<div id="jobModal" class="job-modal-backdrop d-none" role="dialog" aria-modal="true" aria-labelledby="jobModalTitle">
    <div class="job-modal-panel">
        <div class="job-modal-head">
            <div>
                <div id="jobModalTitle" class="fw-bold"><i class="fas fa-clipboard-check text-info me-2"></i>Web-Installer-Job</div>
                <div id="jobModalSubtitle" class="text-secondary small mt-1">Bereit.</div>
            </div>
            <button class="btn btn-sm btn-outline-secondary" type="button" onclick="hideJobModal()" aria-label="Schließen"><i class="fas fa-xmark"></i></button>
        </div>
        <div id="jobModalBody" class="job-modal-body">
            <div class="text-secondary">Noch kein Job gestartet.</div>
        </div>
        <div class="job-modal-actions">
            <button class="btn btn-sm btn-outline-info" type="button" onclick="runGlobalAction('job_status')"><i class="fas fa-clipboard-list me-1"></i>Status unten anzeigen</button>
            <button class="btn btn-sm btn-outline-secondary" type="button" onclick="hideJobModal()">Schließen</button>
        </div>
    </div>
</div>

<div id="configModal" class="job-modal-backdrop d-none" role="dialog" aria-modal="true" aria-labelledby="configModalTitle">
    <div class="job-modal-panel">
        <div class="job-modal-head">
            <div>
                <div id="configModalTitle" class="fw-bold"><i class="fas fa-sliders text-info me-2"></i>Modul-Konfiguration</div>
                <div id="configModalSubtitle" class="text-secondary small mt-1">Relevante Variablen für dieses Modul.</div>
            </div>
            <button class="btn btn-sm btn-outline-secondary" type="button" onclick="hideConfigModal()" aria-label="Schließen"><i class="fas fa-xmark"></i></button>
        </div>
        <div id="configModalBody" class="job-modal-body">
            <div class="text-secondary">Noch kein Modul ausgewählt.</div>
        </div>
        <div class="job-modal-actions">
            <button id="configModalSaveButton" class="btn btn-sm btn-info text-dark fw-bold" type="button" onclick="saveConfigModal()" disabled>
                <i class="fas fa-floppy-disk me-1"></i>Speichern
            </button>
            <a id="configModalOpenLink" class="btn btn-sm btn-outline-info" href="index.php?seite=config">
                <i class="fas fa-arrow-up-right-from-square me-1"></i>In Config öffnen
            </a>
            <button class="btn btn-sm btn-outline-secondary" type="button" onclick="hideConfigModal()">Schließen</button>
        </div>
    </div>
</div>

<div id="diagnosticBundleModal" class="job-modal-backdrop d-none" role="dialog" aria-modal="true" aria-labelledby="diagnosticBundleTitle">
    <div class="job-modal-panel wide">
        <div class="job-modal-head">
            <div>
                <div id="diagnosticBundleTitle" class="fw-bold"><i class="fas fa-file-zipper text-warning me-2"></i>Diagnosepaket erstellen</div>
                <div id="diagnosticBundleSubtitle" class="text-secondary small mt-1">Dateien auswählen, Datenschutzfilter anwenden und Zip herunterladen.</div>
            </div>
            <button class="btn btn-sm btn-outline-secondary" type="button" onclick="hideDiagnosticBundleModal()" aria-label="Schließen"><i class="fas fa-xmark"></i></button>
        </div>
        <div id="diagnosticBundleBody" class="job-modal-body">
            <div class="text-secondary"><i class="fas fa-spinner fa-spin me-1"></i>Lade Dateiliste...</div>
        </div>
        <div class="job-modal-actions">
            <button class="btn btn-sm btn-outline-info" type="button" onclick="selectDiagnosticItems(true)">Alle auswählen</button>
            <button class="btn btn-sm btn-outline-secondary" type="button" onclick="selectDiagnosticItems(false)">Alle abwählen</button>
            <button id="diagnosticBundleDownloadButton" class="btn btn-sm btn-warning text-dark fw-bold" type="button" onclick="downloadDiagnosticBundle()" disabled>
                <i class="fas fa-download me-1"></i>Zip herunterladen
            </button>
            <button class="btn btn-sm btn-outline-secondary" type="button" onclick="hideDiagnosticBundleModal()">Schließen</button>
        </div>
    </div>
</div>

<script src="<?= getAssetUrl('pv_forecast_diagnostics.js') ?>" defer></script>
<script>
const installCenterCsrfToken = <?= json_encode(installCenterCsrfToken(), JSON_UNESCAPED_UNICODE) ?>;
const serviceControlCsrfToken = <?= json_encode(e3dcCsrfToken(), JSON_UNESCAPED_UNICODE) ?>;
const groupLabels = {
    core: ['Kernsystem', 'fa-microchip'],
    consumers: ['Verbraucher', 'fa-plug-circle-bolt'],
    integrations: ['Integrationen', 'fa-link'],
    system: ['System', 'fa-server']
};
let installerStatusRefreshActive = false;
let jobModalRefreshTimer = null;
let installCenterModules = {};
let installCenterDiagnosis = {};
let installCenterReadiness = {};
let configModalCurrentModule = '';
let diagnosticBundleManifest = null;
const DIAGNOSTIC_FORUM_LIMIT_BYTES = 1024 * 1024;

function esc(value) {
    return normalizeDisplayText(value).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));
}

function normalizeDisplayText(value) {
    return String(value ?? '')
        .replace(/Ã„/g, 'Ä')
        .replace(/Ã–/g, 'Ö')
        .replace(/Ãœ/g, 'Ü')
        .replace(/Ã¤/g, 'ä')
        .replace(/Ã¶/g, 'ö')
        .replace(/Ã¼/g, 'ü')
        .replace(/ÃŸ/g, 'ß')
        .replace(/Â·/g, '·')
        .replace(/Â /g, ' ')
        .replace(/â†’/g, '→')
        .replace(/â€“/g, '–')
        .replace(/â€”/g, '—')
        .replace(/â€¦/g, '…')
        .replace(/â‚¬/g, '€')
        .replace(/Waermefreigabe/g, 'Wärmefreigabe')
        .replace(/Waermepumpen/g, 'Wärmepumpen')
                .replace(/W\u0061ermepumpe/g, 'Wärmepumpe')
        .replace(/Waerme/g, 'Wärme')
                .replace(/f\u0075er/g, 'für')
                .replace(/\u0075eber/g, 'über')
        .replace(/zusaetzlich/g, 'zusätzlich')
        .replace(/guenstig/g, 'günstig')
        .replace(/noetig/g, 'nötig')
                .replace(/moegl\u0069ch/g, 'möglich');
}

function serviceKey(unit) {
    return String(unit || '').endsWith('.service') ? unit : `${unit}.service`;
}

function statusClass(ok, warn = false) {
    if (ok) return 'ok';
    return warn ? 'warn' : 'bad';
}

function statusText(module, serviceInfo, diagnosis) {
    const service = serviceInfo || {};
    const diag = diagnosis || {};
    const alive = diag.alive || {};
    const cfg = diag.config || {};
    const active = Boolean(service.active || (diag.systemd && diag.systemd.active));
    const installed = Boolean(service.exists || (diag.systemd && diag.systemd.exists));
    const enabledRaw = String(service.enabled_raw || (diag.systemd && diag.systemd.enabled_raw) || '').trim();
    const enabledKnown = typeof service.enabled_known === 'boolean'
        ? service.enabled_known
        : ['enabled', 'disabled'].includes(enabledRaw);
    const enabled = enabledKnown
        && enabledRaw === 'enabled'
        && Boolean(service.enabled || (diag.systemd && diag.systemd.enabled));
    const fresh = Boolean(alive.fresh || (!module.alive_file && active));
    const cfgOk = cfg.ok !== false;
    return {
        active,
        installed,
        enabled,
        enabledKnown,
        enabledRaw,
        fresh,
        cfgOk,
        age: alive.age_s,
        raw: service.raw_status || (diag.systemd && diag.systemd.raw) || 'unbekannt'
    };
}

function renderModuleReadiness(installBlock) {
    if (!installBlock || !installBlock.readiness) return '';
    const readiness = installBlock.readiness || {};
    const state = readiness.state || 'unknown';
    const labels = {
        ready: 'Installation möglich',
        installed: 'Fertig eingerichtet',
        blocked: 'Blockiert',
        docker_pending: 'Docker-Ablauf nötig',
        unknown: 'Installationsstatus unklar'
    };
    const icons = {
        ready: 'fa-circle-check ok',
        installed: 'fa-circle-check text-primary',
        blocked: 'fa-circle-xmark bad',
        docker_pending: 'fa-box warn',
        unknown: 'fa-circle-info text-info'
    };
    const messages = {
        ready: 'Mit späterer Schreibfreigabe installierbar.',
        installed: 'Dienst ist vorhanden und braucht keine Installation.',
        blocked: 'Bitte erst Konfiguration oder Dateien prüfen.',
        docker_pending: 'Docker: Config speichern und Container neu starten.',
        unknown: 'Bitte Details prüfen.'
    };
    const reason = (readiness.reasons || installBlock.blocked_reasons || [])[0];
    return `
        <div class="module-readiness ${esc(state)}">
            <strong><i class="fas ${icons[state] || icons.unknown} me-1"></i>${esc(labels[state] || labels.unknown)}</strong>
            <span class="text-secondary">${esc(reason || readiness.message || messages[state] || messages.unknown)}</span>
        </div>
    `;
}

function focusKeyFromConfigName(name) {
    const raw = String(name || '').trim();
    if (!raw) return '';
    if (raw.includes(' oder ')) return raw.split(' oder ')[0].trim();
    if (raw.includes('=')) return raw.split('=')[0].trim();
    return raw;
}

function firstConfigFocusKey(module, config = {}) {
    const missing = (config.missing_keys || []).map(focusKeyFromConfigName).filter(Boolean);
    if (missing.length) return missing[0];
    const required = (config.required_config_keys || []).map(focusKeyFromConfigName).filter(Boolean);
    if (required.length) return required[0];
    const keys = module && module.config_keys ? module.config_keys.map(focusKeyFromConfigName).filter(Boolean) : [];
    return keys[0] || '';
}

function moduleConfigUrl(module, config = {}) {
    const keys = module && module.config_keys ? module.config_keys : [];
    const base = 'index.php?seite=config';
    const focusKey = firstConfigFocusKey(module, config);
    if (!keys.length && !focusKey) return base;
    return `${base}&focus=${encodeURIComponent(focusKey)}`;
}

function renderConfigButton(module, config = {}, classes = 'btn btn-sm btn-outline-info', label = 'Konfig') {
    if (!module || !module.config_keys || !module.config_keys.length) return '';
    return `<button type="button" class="${esc(classes)}" onclick="showConfigModal('${esc(module.key)}')"><i class="fas fa-sliders me-1"></i>${esc(label)}</button>`;
}

function uniqueItems(items) {
    return Array.from(new Set((items || []).filter(Boolean)));
}

function renderConfigChips(items, cls) {
    return uniqueItems(items).map(item => `<span class="config-field-chip ${esc(cls)}">${esc(item)}</span>`).join('');
}

function configDisplayList(config, labelsKey, rawKey) {
    const labels = config && Array.isArray(config[labelsKey]) ? config[labelsKey] : [];
    return labels.length ? labels : ((config && Array.isArray(config[rawKey])) ? config[rawKey] : []);
}

function renderConfigInput(field) {
    const key = String(field.key || '');
    const value = String(field.value ?? '');
    const type = field.type || 'text';
    const placeholder = field.secret && field.has_value
        ? 'gesetzt - leer lassen = unverändert'
        : (field.placeholder || '');
    if (type === 'select') {
        const options = (field.options || []).map(opt => {
            const optValue = String(opt.value ?? '');
            const disabled = opt.disabled ? 'disabled' : '';
            return `<option value="${esc(optValue)}" ${optValue === value ? 'selected' : ''} ${disabled}>${esc(opt.label || optValue)}</option>`;
        }).join('');
        return `<select class="form-select" name="values[${esc(key)}]">${options}</select>`;
    }
    const inputType = type === 'password' ? 'password' : (type === 'time' ? 'time' : (type === 'number' ? 'number' : 'text'));
    const step = type === 'number' ? ' step="any"' : '';
    const current = field.secret ? '' : value;
    return `<input class="form-control" type="${inputType}"${step} name="values[${esc(key)}]" value="${esc(current)}" placeholder="${esc(placeholder)}">`;
}

function renderConfigEditor(payload) {
    const fields = payload.fields || [];
    if (!fields.length) {
        return '<div class="text-secondary mt-3">Für dieses Modul sind noch keine direkten Config-Felder freigegeben.</div>';
    }
    const formFields = fields.map(field => `
        <div class="config-edit-field">
            <label for="cfg_${esc(field.key)}">${esc(field.label || field.key)}</label>
            ${renderConfigInput(field)}
            <div class="config-edit-help">
                <span class="small-code">${esc(field.key)}</span>${field.help ? ` - ${esc(field.help)}` : ''}
            </div>
        </div>
    `).join('');
    return `
        <form id="configModalForm" class="mt-3">
            <div class="d-flex align-items-center justify-content-between gap-2 flex-wrap">
                <strong><i class="fas fa-pen-to-square text-info me-1"></i>Direkt bearbeiten</strong>
                <span class="text-secondary small">Backup wird vor dem Speichern automatisch angelegt.</span>
            </div>
            <div class="config-edit-grid">${formFields}</div>
            <div id="configModalSaveState" class="config-save-state text-secondary"></div>
        </form>
    `;
}

async function loadConfigModalFields(moduleKey) {
    const saveButton = document.getElementById('configModalSaveButton');
    const editor = document.getElementById('configModalEditor');
    if (!editor) return;
    saveButton.disabled = true;
    editor.innerHTML = '<div class="text-secondary mt-3"><i class="fas fa-spinner fa-spin me-1"></i>Lade aktuelle Config-Werte...</div>';
    try {
        const payload = await loadJson(`install_center.php?action=module_config&module=${encodeURIComponent(moduleKey)}`);
        if (!payload.success) {
            editor.innerHTML = `<div class="warn mt-3">${esc(payload.error || 'Config-Felder konnten nicht geladen werden.')}</div>`;
            return;
        }
        editor.innerHTML = renderConfigEditor(payload);
        saveButton.disabled = !(payload.fields || []).length;
    } catch (err) {
        editor.innerHTML = `<div class="bad mt-3">Config-Felder konnten nicht geladen werden: ${esc(err.message || err)}</div>`;
    }
}

async function saveConfigModal() {
    const moduleKey = configModalCurrentModule;
    const form = document.getElementById('configModalForm');
    const state = document.getElementById('configModalSaveState');
    const saveButton = document.getElementById('configModalSaveButton');
    if (!moduleKey || !form) return;
    const body = new FormData(form);
    body.append('module', moduleKey);
    body.append('csrf_token', installCenterCsrfToken);
    saveButton.disabled = true;
    if (state) state.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Speichere...';
    try {
        const res = await fetch('install_center.php?action=save_module_config', {method: 'POST', body});
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (!data.success) {
            if (state) state.innerHTML = `<span class="bad"><i class="fas fa-circle-xmark me-1"></i>${esc(data.error || 'Speichern fehlgeschlagen')}</span>`;
            return;
        }
        if (state) {
            state.innerHTML = `<span class="ok"><i class="fas fa-check me-1"></i>${esc(data.message || 'Gespeichert.')}</span>`;
        }
        document.getElementById('actionLog').innerHTML = `
            <div class="result-title"><i class="fas fa-check-circle ok"></i>Konfiguration gespeichert</div>
            <div>${esc(data.message || 'Werte gespeichert.')}</div>
            ${data.updated_keys ? `<div class="small-code mt-1">${esc(data.updated_keys.join(', '))}</div>` : ''}
        `;
        await loadInstallCenter();
        await loadConfigModalFields(moduleKey);
    } catch (err) {
        if (state) state.innerHTML = `<span class="bad"><i class="fas fa-circle-xmark me-1"></i>${esc(err.message || err)}</span>`;
    } finally {
        saveButton.disabled = false;
    }
}

function showConfigModal(moduleKey) {
    const module = installCenterModules[moduleKey] || {};
    const diagnosis = installCenterDiagnosis[moduleKey] || {};
    const installBlock = installCenterReadiness[moduleKey] || {};
    const config = diagnosis.config || installBlock.config || {};
    const missing = config.missing_keys || [];
    const missingDisplay = configDisplayList(config, 'missing_labels', 'missing_keys');
    const required = config.required_config_keys || [];
    const requiredDisplay = configDisplayList(config, 'required_config_labels', 'required_config_keys');
    const focus = module.config_keys || [];
    const focusDisplay = configDisplayList(config, 'config_key_labels', 'config_keys');
    const modal = document.getElementById('configModal');
    const title = document.getElementById('configModalTitle');
    const subtitle = document.getElementById('configModalSubtitle');
    const body = document.getElementById('configModalBody');
    const link = document.getElementById('configModalOpenLink');
    const saveButton = document.getElementById('configModalSaveButton');
    configModalCurrentModule = moduleKey;
    saveButton.disabled = true;
    const missingHtml = missing.length
        ? `<div class="mt-3"><strong class="warn"><i class="fas fa-triangle-exclamation me-1"></i>Noch offen</strong><div class="config-field-list">${renderConfigChips(missingDisplay, 'missing')}</div></div>`
        : `<div class="ok mt-3"><i class="fas fa-check me-1"></i>Die Pflichtwerte für den aktuell gewählten Modultyp wirken vollständig.</div>`;
    const requiredHtml = required.length
        ? `<div class="mt-3"><strong><i class="fas fa-list-check text-info me-1"></i>Pflichtwerte für den aktuellen Typ</strong><div class="config-field-list">${renderConfigChips(requiredDisplay, 'required')}</div></div>`
        : `<div class="mt-3 text-secondary">Dieses Modul hat keine festen Pflichtwerte.</div>`;
    const focusHtml = focus.length
        ? `<div class="mt-3"><strong><i class="fas fa-crosshairs text-secondary me-1"></i>Weitere relevante Config-Felder</strong><div class="config-field-list">${renderConfigChips(focusDisplay, 'focus')}</div></div>`
        : '';
    title.innerHTML = `<i class="fas fa-sliders text-info me-2"></i>${esc(module.display_name || moduleKey || 'Modul-Konfiguration')}`;
    subtitle.textContent = module.service_unit || module.service || 'Relevante Variablen für dieses Modul.';
    body.innerHTML = `
        <div>${esc(module.description || 'Diese Variablen bedingen, ob das Modul sinnvoll installiert oder gestartet werden kann.')}</div>
        ${missingHtml}
        ${requiredHtml}
        ${focusHtml}
        <div class="text-secondary small mt-3">
            Du kannst die freigegebenen Modulwerte direkt hier setzen. Tokens und Passwörter werden nicht angezeigt;
            leere Secret-Felder bleiben beim Speichern unverändert.
        </div>
        <div id="configModalEditor"></div>
    `;
    link.href = moduleConfigUrl(module, config);
    modal.classList.remove('d-none');
    loadConfigModalFields(moduleKey);
}

function hideConfigModal() {
    document.getElementById('configModal').classList.add('d-none');
    configModalCurrentModule = '';
}

function formatBytes(bytes) {
    const value = Number(bytes || 0);
    if (!value) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    let size = value;
    let idx = 0;
    while (size >= 1024 && idx < units.length - 1) {
        size /= 1024;
        idx += 1;
    }
    return `${size.toFixed(idx === 0 ? 0 : 1)} ${units[idx]}`;
}

function updateDiagnosticDownloadButton() {
    const boxes = Array.from(document.querySelectorAll('.diagnostic-item-checkbox:checked'));
    const selected = boxes.length;
    const approxBytes = boxes.reduce((sum, box) => sum + Number(box.dataset.bundleSize || 0), 0);
    const button = document.getElementById('diagnosticBundleDownloadButton');
    const sizeHint = document.getElementById('diagnosticBundleSizeHint');
    if (!button) return;
    button.disabled = selected === 0;
    button.innerHTML = selected
        ? `<i class="fas fa-download me-1"></i>Zip herunterladen (${selected}, ca. ${esc(formatBytes(approxBytes))})`
        : '<i class="fas fa-download me-1"></i>Zip herunterladen';
    if (sizeHint) {
        if (!selected) {
            sizeHint.className = 'text-secondary small mt-2';
            sizeHint.innerHTML = '';
        } else if (approxBytes > DIAGNOSTIC_FORUM_LIMIT_BYTES) {
            sizeHint.className = 'alert alert-warning py-2 px-3 mt-2 mb-0';
            sizeHint.innerHTML = `<i class="fas fa-triangle-exclamation me-1"></i>Auswahl ca. ${esc(formatBytes(approxBytes))}; Forum-Limit ist ${esc(formatBytes(DIAGNOSTIC_FORUM_LIMIT_BYTES))}. Für das Forum das Preset „Forum kompakt“ verwenden oder Roh-Historien abwählen.`;
        } else {
            sizeHint.className = 'text-secondary small mt-2';
            sizeHint.innerHTML = `<i class="fas fa-check-circle text-success me-1"></i>Auswahl liegt voraussichtlich unter dem Forum-Limit von ${esc(formatBytes(DIAGNOSTIC_FORUM_LIMIT_BYTES))}.`;
        }
    }
}

function renderDiagnosticPresets(payload) {
    const presets = payload.presets || [];
    if (!presets.length) return '';
    const buttons = presets.map(preset => {
        const limit = Number(preset.forum_limit_bytes || 0);
        const forumText = limit
            ? (preset.forum_safe ? ` · Forumlimit ${formatBytes(limit)}` : ` · > ${formatBytes(limit)}`)
            : '';
        return `
        <button class="diagnostic-preset-button" type="button" data-preset-id="${esc(preset.id)}" onclick="applyDiagnosticPreset(this.dataset.presetId)">
            <div class="diagnostic-preset-title"><i class="fas ${esc(preset.icon || 'fa-file-zipper')} text-info"></i>${esc(preset.label || preset.id)}</div>
            <div class="diagnostic-preset-desc">${esc(preset.description || '')}</div>
            <div class="diagnostic-preset-meta">${esc(preset.item_count || 0)} Dateien, ca. ${esc(formatBytes(preset.bundle_size || 0))}${esc(forumText)}</div>
        </button>
    `;
    }).join('');
    return `
        <div class="mt-3">
            <div class="fw-bold"><i class="fas fa-layer-group text-info me-1"></i>Vorauswahl nach Fehlerbild</div>
            <div class="text-secondary small">Ein Preset setzt die passenden Haken. Danach kann die Auswahl weiter manuell angepasst werden.</div>
            <div class="diagnostic-preset-grid">${buttons}</div>
        </div>
    `;
}

function applyDiagnosticPreset(presetId) {
    const preset = (diagnosticBundleManifest && diagnosticBundleManifest.presets || []).find(entry => entry.id === presetId);
    if (!preset) return;
    const selected = new Set(preset.items || []);
    document.querySelectorAll('.diagnostic-item-checkbox').forEach(box => {
        box.checked = selected.has(box.value);
    });
    document.querySelectorAll('.diagnostic-preset-button').forEach(button => {
        button.classList.toggle('active', button.dataset.presetId === presetId);
    });
    updateDiagnosticDownloadButton();
}

function clearDiagnosticPresetSelection() {
    document.querySelectorAll('.diagnostic-preset-button').forEach(button => button.classList.remove('active'));
}

function renderDiagnosticManifest(payload) {
    if (!payload || payload.success === false) {
        return `<div class="bad">Dateiliste konnte nicht geladen werden: ${esc(payload && (payload.error || payload.message) || 'keine Antwort')}</div>`;
    }
    const items = payload.items || [];
    const rows = items.map((item, idx) => `
        <div class="diagnostic-file-item">
            <label>
                <input class="form-check-input diagnostic-item-checkbox" type="checkbox" value="${esc(item.id)}" data-bundle-size="${esc(item.bundle_size || item.size || 0)}" ${item.default ? 'checked' : ''} onchange="clearDiagnosticPresetSelection(); updateDiagnosticDownloadButton()">
                <span>
                    <strong>${esc(item.label || item.id)}</strong>
                    <div class="diagnostic-file-meta">
                        ${esc(item.kind || 'Datei')} ${item.size ? ` · Original ${esc(formatBytes(item.size))}` : ''}${item.bundle_size ? ` · im Paket ca. ${esc(formatBytes(item.bundle_size))}` : ''}<br>
                        <span class="small-code">${esc(item.path || '')}</span><br>
                        ${esc(item.privacy || '')}
                    </div>
                </span>
            </label>
        </div>
    `).join('');
    return `
        <div class="privacy-box">
            <strong><i class="fas fa-user-shield text-info me-1"></i>Datenschutz</strong>
            <div class="small mt-1">${esc(payload.privacy_note || 'Das Paket wird lokal erzeugt und automatisch bereinigt.')}</div>
            <div class="small mt-1">Empfehlung: Paket vor dem Versenden einmal öffnen. LAN-IPs, Dienstnamen und technische Zustände bleiben absichtlich enthalten, weil sie bei der Fehlersuche helfen.</div>
        </div>
        <div class="row g-2 mt-2 align-items-end">
            <div class="col-md-7">
                <label class="form-label small" for="diagnosticIncidentAt">Vorfallszeitpunkt</label>
                <input id="diagnosticIncidentAt" class="form-control form-control-sm" type="datetime-local" step="1">
            </div>
            <div class="col-md-5">
                <label class="form-label small" for="diagnosticIncidentWindow">Zeitfenster</label>
                <select id="diagnosticIncidentWindow" class="form-select form-select-sm">
                    <option value="1800:600" selected>30 min davor / 10 min danach</option>
                    <option value="3600:1800">60 min davor / 30 min danach</option>
                    <option value="21600:7200">6 h davor / 2 h danach</option>
                </select>
            </div>
        </div>
        ${renderDiagnosticPresets(payload)}
        <div id="diagnosticBundleSizeHint" class="text-secondary small mt-2"></div>
        <div class="diagnostic-file-list">${rows || '<div class="text-secondary">Keine Diagnose-Dateien gefunden.</div>'}</div>
    `;
}

async function showDiagnosticBundleModal() {
    const modal = document.getElementById('diagnosticBundleModal');
    const body = document.getElementById('diagnosticBundleBody');
    const button = document.getElementById('diagnosticBundleDownloadButton');
    diagnosticBundleManifest = null;
    if (button) button.disabled = true;
    body.innerHTML = '<div class="text-secondary"><i class="fas fa-spinner fa-spin me-1"></i>Lade Dateiliste...</div>';
    modal.classList.remove('d-none');
    try {
        const payload = await loadJson('install_center.php?action=diagnostic_manifest');
        diagnosticBundleManifest = payload;
        body.innerHTML = renderDiagnosticManifest(payload);
        const incidentInput = document.getElementById('diagnosticIncidentAt');
        if (incidentInput) {
            const localNow = new Date(Date.now() - new Date().getTimezoneOffset() * 60000);
            incidentInput.value = localNow.toISOString().slice(0, 19);
        }
        const forumPreset = (payload.presets || []).find(entry => entry.id === 'standard');
        if (forumPreset) {
            applyDiagnosticPreset('standard');
        } else {
            updateDiagnosticDownloadButton();
        }
    } catch (err) {
        body.innerHTML = `<div class="bad">Dateiliste konnte nicht geladen werden: ${esc(err.message || err)}</div>`;
    }
}

function hideDiagnosticBundleModal() {
    document.getElementById('diagnosticBundleModal').classList.add('d-none');
}

function selectDiagnosticItems(value) {
    document.querySelectorAll('.diagnostic-item-checkbox').forEach(box => {
        box.checked = Boolean(value);
    });
    document.querySelectorAll('.diagnostic-preset-button').forEach(button => button.classList.remove('active'));
    updateDiagnosticDownloadButton();
}

async function downloadDiagnosticBundle() {
    const checked = Array.from(document.querySelectorAll('.diagnostic-item-checkbox:checked')).map(box => box.value);
    const body = document.getElementById('diagnosticBundleBody');
    const button = document.getElementById('diagnosticBundleDownloadButton');
    if (!checked.length) {
        body.insertAdjacentHTML('afterbegin', '<div class="bad mb-2">Bitte mindestens eine Datei auswählen.</div>');
        return;
    }
    const form = new FormData();
    form.append('csrf_token', installCenterCsrfToken);
    checked.forEach(id => form.append('items[]', id));
    const incidentInput = document.getElementById('diagnosticIncidentAt');
    const incidentDate = incidentInput && incidentInput.value ? new Date(incidentInput.value) : new Date();
    form.append('incident_ts', String(Math.floor(incidentDate.getTime() / 1000)));
    const windowSelect = document.getElementById('diagnosticIncidentWindow');
    const windowParts = String(windowSelect && windowSelect.value || '1800:600').split(':');
    form.append('incident_before_s', windowParts[0] || '1800');
    form.append('incident_after_s', windowParts[1] || '600');
    const oldLabel = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Erstelle Zip...';
    try {
        const res = await fetch('install_center.php?action=diagnostic_bundle', {method: 'POST', body: form});
        const contentType = res.headers.get('content-type') || '';
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        if (contentType.includes('application/json')) {
            const data = await res.json();
            throw new Error(data.error || data.message || 'Diagnosepaket konnte nicht erstellt werden');
        }
        const blob = await res.blob();
        const disposition = res.headers.get('content-disposition') || '';
        const match = disposition.match(/filename="?([^"]+)"?/i);
        const fileName = match ? match[1] : 'e3dc_diagnose.zip';
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = fileName;
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
    } catch (err) {
        const errorHtml = `<div class="alert alert-danger mt-2 mb-0">Diagnosepaket konnte nicht erstellt werden: ${esc(err.message || err)}</div>`;
        body.insertAdjacentHTML('afterbegin', errorHtml);
    } finally {
        button.disabled = false;
        button.innerHTML = oldLabel;
        updateDiagnosticDownloadButton();
    }
}

function moduleHasConfigBlocker(installBlock) {
    const readiness = installBlock && installBlock.readiness ? installBlock.readiness : {};
    const reasons = readiness.reasons || (installBlock && installBlock.blocked_reasons) || [];
    return reasons.some(reason => String(reason).toLowerCase().includes('konfiguration'));
}

function renderModuleNextAction(module, installBlock) {
    const readiness = installBlock && installBlock.readiness ? installBlock.readiness : {};
    const state = readiness.state || 'unknown';
    const hasConfigBlocker = moduleHasConfigBlocker(installBlock);
    if (state === 'installed') {
        return '<button class="btn btn-sm btn-outline-primary" disabled><i class="fas fa-check me-1"></i>Fertig</button>';
    }
    if (state === 'ready') {
        return `
            <button class="btn btn-sm btn-outline-primary" title="Sicheren Ramdisk-Job testen, noch ohne echte Installation" onclick="runModuleJob('${esc(module.key)}','install_module_dry_run')"><i class="fas fa-clipboard-check me-1"></i>Job-Test</button>
            <button class="btn btn-sm btn-outline-success" title="Zeigt Voraussetzungen und den sicheren Installationsbedarf ohne Systemänderung" onclick="runModuleAction('${esc(module.key)}','install_module_dry_run')"><i class="fas fa-box-open me-1"></i>Installationsdetails</button>
        `;
    }
    if (state === 'blocked' && hasConfigBlocker && module.config_keys && module.config_keys.length) {
        return renderConfigButton(module, installBlock.config || {}, 'btn btn-sm btn-outline-warning', 'Einrichten');
    }
    if (state === 'blocked') {
        return `<button class="btn btn-sm btn-outline-warning" onclick="runModuleAction('${esc(module.key)}','install_module_dry_run')"><i class="fas fa-triangle-exclamation me-1"></i>Blocker anzeigen</button>`;
    }
    if (state === 'docker_pending') {
        return `<button class="btn btn-sm btn-outline-warning" onclick="runModuleAction('${esc(module.key)}','install_module_dry_run')"><i class="fas fa-box me-1"></i>Docker-Hinweis</button>`;
    }
    return `<button class="btn btn-sm btn-outline-secondary" onclick="runModuleAction('${esc(module.key)}','install_module_dry_run')"><i class="fas fa-circle-info me-1"></i>Details</button>`;
}

function renderModuleQuickServiceAction(unit, state, isCore, canService) {
    if (!canService || !state.installed) return '';
    if (unit === 'e3dc-forecast-evidence.service') {
        if (state.active && state.enabledKnown && state.enabledRaw === 'enabled') {
            return `<button class="btn btn-sm btn-outline-primary" disabled><i class="fas fa-circle-check me-1"></i>Dauerhaft aktiv</button>`;
        }
        return `<button class="btn btn-sm btn-success" title="Aktiviert den Autostart und startet die rein diagnostische Unit als feste Transaktion" onclick="controlService('${esc(unit)}','activate_forecast_evidence')"><i class="fas fa-toggle-on me-1"></i>Aktivieren &amp; starten</button>`;
    }
    if (state.active) {
        return `<button class="btn btn-sm btn-outline-info" onclick="controlService('${esc(unit)}','restart')"><i class="fas fa-rotate me-1"></i>Neustart</button>`;
    }
    if (isCore) {
        return `<button class="btn btn-sm btn-outline-info" title="Kernmodule werden hier nur neu gestartet; systemd restart startet auch inaktive Units wieder" onclick="controlService('${esc(unit)}','restart')"><i class="fas fa-rotate me-1"></i>Neustart</button>`;
    }
    if (!isCore) {
        return `<button class="btn btn-sm btn-success" onclick="controlService('${esc(unit)}','start')"><i class="fas fa-play me-1"></i>Start</button>`;
    }
    return '';
}

function renderModule(module, serviceInfo, diagnosis, installBlock = null) {
    const state = statusText(module, serviceInfo, diagnosis);
    const config = diagnosis && diagnosis.config ? diagnosis.config : {};
    const unit = serviceKey(module.service_unit);
    const isCore = module.group === 'core' && module.optional === false;
    const isForecastEvidenceUnit = unit === 'e3dc-forecast-evidence.service';
    const canService = module.actions && module.actions.some(a => ['start','stop','restart','enable','disable'].includes(a));
    const startDisabled = isForecastEvidenceUnit
        ? 'disabled title="Bitte ausschließlich Aktivieren & starten verwenden"'
        : (isCore
        ? 'disabled title="Kernmodule werden hier nur diagnostiziert oder neu gestartet"'
        : (!canService
            ? 'disabled title="Dienststeuerung für dieses Modul nicht vorgesehen"'
            : (!state.installed
                ? 'disabled title="Dienst fehlt noch: erst Installations-Check oder Job-Test ausführen"'
                : (state.active ? 'disabled title="Dienst läuft bereits"' : ''))));
    const restartDisabled = isForecastEvidenceUnit
        ? 'disabled title="Die Prognosediagnose wird nur über den gebundenen Aktivierungspfad geändert"'
        : (!canService
        ? 'disabled title="Dienststeuerung für dieses Modul nicht vorgesehen"'
        : (!state.installed
            ? 'disabled title="Dienst fehlt noch: erst Installations-Check oder Job-Test ausführen"'
            : (isCore ? '' : (state.active ? '' : 'disabled title="Dienst ist inaktiv: bitte Start verwenden"'))));
    const stopDisabled = isForecastEvidenceUnit
        ? 'disabled title="Die Prognosediagnose wird nur über den gebundenen Aktivierungspfad geändert"'
        : (isCore
        ? 'disabled title="Kernmodule werden hier nicht gestoppt"'
        : (!canService
            ? 'disabled title="Dienststeuerung für dieses Modul nicht vorgesehen"'
            : (!state.installed
                ? 'disabled title="Dienst fehlt noch: Stop ist nicht nötig"'
                : (state.active ? '' : 'disabled title="Dienst läuft nicht"'))));
    const hasConfigBlocker = moduleHasConfigBlocker(installBlock);
    const configLink = renderConfigButton(module, config, 'btn btn-sm btn-outline-info', 'Modul-Config');
    const quickServiceAction = renderModuleQuickServiceAction(unit, state, isCore, canService);
    const configQuickLink = (!state.cfgOk && !hasConfigBlocker)
        ? renderConfigButton(module, config, 'btn btn-sm btn-outline-warning', 'Einrichten')
        : '';
    const configKeyDisplay = configDisplayList(config, 'config_key_labels', 'config_keys');
    const configKeyText = configKeyDisplay.length ? configKeyDisplay : (module.config_keys || []);
    const warningBadge = module.install_warning
        ? `<span class="badge text-bg-warning" title="${esc(module.install_warning)}">Hinweis</span>`
        : '';
    return `
        <article class="module-card ${esc(module.group)}">
            <div class="module-title">
                <h2>${esc(module.display_name)}</h2>
                <span class="badge ${module.optional ? 'text-bg-secondary' : 'text-bg-primary'}">${module.optional ? 'Optional' : 'Kern'}</span>
                ${warningBadge}
            </div>
            <div class="desc">${esc(module.description)}</div>
            <div class="small-code mt-2">${esc(unit)}</div>
            <div class="status-row">
                <div class="status-pill">
                    Dienst
                    <strong class="${statusClass(state.active, state.installed)}">${state.active ? 'aktiv' : (state.installed ? 'inaktiv' : 'fehlt')}</strong>
                    <span class="text-secondary">${esc(state.raw)}</span>
                </div>
                <div class="status-pill">
                    Daten
                    <strong class="${statusClass(state.fresh, state.age !== null && state.age !== undefined)}">${state.fresh ? 'frisch' : (state.age !== null && state.age !== undefined ? 'alt' : 'kein Signal')}</strong>
                    <span class="text-secondary">${state.age !== null && state.age !== undefined ? `${state.age}s` : 'keine Datei'}</span>
                </div>
                <div class="status-pill">
                    Konfig
                    <strong class="${statusClass(state.cfgOk, true)}">${state.cfgOk ? 'OK' : 'offen'}</strong>
                    <span class="text-secondary">${configKeyText.length ? esc(configKeyText.join(', ')) : 'keine Pflichtfelder'}</span>
                </div>
            </div>
            ${renderModuleReadiness(installBlock)}
            <div class="primary-actions">
                <button class="btn btn-sm btn-outline-light" title="Öffnet Status, Config, Log- und Journal-Vorschau für dieses Modul" onclick="showModuleDiagnosis('${esc(module.key)}')"><i class="fas fa-stethoscope me-1"></i> Diagnose</button>
                ${renderModuleNextAction(module, installBlock)}
                ${quickServiceAction}
                ${configQuickLink}
            </div>
            <details class="advanced-actions">
                <summary><i class="fas fa-screwdriver-wrench me-1"></i>Expertenaktionen</summary>
                <div class="module-actions">
                    <button class="btn btn-sm btn-outline-warning" title="Direkter Dry-Run: prüft nur und aktualisiert den letzten Job nicht" onclick="runModuleAction('${esc(module.key)}','dry_run')"><i class="fas fa-list-check me-1"></i> Direkt-Dry-Run</button>
                    <button class="btn btn-sm btn-outline-secondary" title="Direkter Installations-Dry-Run: zeigt Bedarf, schreibt aber keinen Jobstatus" onclick="runModuleAction('${esc(module.key)}','install_module_dry_run')"><i class="fas fa-box-open me-1"></i> Install-Details</button>
                    <button class="btn btn-sm btn-success" ${startDisabled} onclick="controlService('${esc(unit)}','start')"><i class="fas fa-play me-1"></i> Start</button>
                    <button class="btn btn-sm btn-outline-info" ${restartDisabled} onclick="controlService('${esc(unit)}','restart')"><i class="fas fa-rotate me-1"></i> Neustart</button>
                    <button class="btn btn-sm btn-outline-danger" ${stopDisabled} onclick="controlService('${esc(unit)}','stop')"><i class="fas fa-stop me-1"></i> Stop</button>
                    ${configLink}
                </div>
            </details>
            <div class="action-hint">Oben stehen die sinnvollen Standardaktionen. Expertenaktionen bleiben erlaubt, sind aber bewusst eingeklappt.</div>
        </article>
    `;
}

function prettyJson(data) {
    return JSON.stringify(data, null, 2);
}

function boolBadge(ok, goodText, badText, warn = false) {
    const cls = ok ? 'text-bg-success' : (warn ? 'text-bg-warning' : 'text-bg-danger');
    return `<span class="badge ${cls}">${esc(ok ? goodText : badText)}</span>`;
}

function firstObject(obj) {
    if (!obj || typeof obj !== 'object') return null;
    const keys = Object.keys(obj);
    return keys.length ? obj[keys[0]] : null;
}

function renderRawDetails(data) {
    return `<details class="mt-2"><summary class="text-secondary small">Rohdaten anzeigen</summary><div class="raw-json">${esc(prettyJson(data))}</div></details>`;
}

function ruleCalmStatusBadge(status) {
    const normalized = String(status || 'UNKNOWN').toUpperCase();
    if (normalized === 'PASS') return '<span class="badge text-bg-success">ruhig</span>';
    if (normalized === 'FAIL') return '<span class="badge text-bg-warning">auffällig</span>';
    return `<span class="badge text-bg-secondary">${esc(normalized || 'unbekannt')}</span>`;
}

function ruleCalmDataQualityBadge(status) {
    const normalized = String(status || 'NOT_ANALYZED').toUpperCase();
    if (normalized === 'PASS') return '<span class="badge text-bg-success">unauffällig</span>';
    if (normalized === 'HINT') return '<span class="badge text-bg-info">Hinweis</span>';
    if (normalized === 'FAIL') return '<span class="badge text-bg-warning">auffällig</span>';
    if (normalized === 'EVIDENCE_LIMIT') return '<span class="badge text-bg-secondary">EVIDENCE_LIMIT</span>';
    return '<span class="badge text-bg-secondary">nicht ausgewertet</span>';
}

function ruleCalmServiceLabel(name) {
    const labels = {storage: 'Speicher', wallbox: 'Wallbox', heatpump: 'Wärmepumpe', ems: 'EMS'};
    return labels[name] || name;
}

function ruleCalmCheckLabel(name) {
    const labels = {
        wallbox_start_stop: 'Wallbox Start/Stop',
        wallbox_phase: 'Wallbox Phasen',
        storage: 'Speicher-Commands',
        storage_owner: 'Speicher-Owner',
        storage_contract_owner: 'Speicher-Contract',
        storage_execution_class: 'Speicher-Ausführung',
        storage_state: 'Speicher-State',
        storage_state_reason: 'Speicher-Grund',
        storage_value_update: 'Speicher-Wertupdate',
        storage_decision_path: 'Speicher-Entscheidungspfad',
        storage_budget_executor_shadow: 'Speicher-Executor',
        storage_live_plausibility: 'Speicher-Messwerte',
        heatpump: 'Wärmepumpe',
        ems: 'EMS',
        ems_decision: 'EMS-Entscheidungen',
        current_history: 'Aktuelle Verlaufsdaten',
        diagnose_zip_upload: 'Diagnose-ZIP'
    };
    return labels[name] || name;
}

function ruleCalmActorLabel(name) {
    const labels = {
        storage_decision_path: 'Speicher-Entscheidungspfad'
    };
    return labels[name] || name;
}

function ruleCalmActionLabel(name) {
    const labels = {
        PROTECTION: 'Schutzpfad',
        CURVE: 'Ladekurve',
        DIRECT_MARKETING: 'Direktvermarktung',
        MARKET_DIRECT: 'Direktvermarktung',
        MARKET_PRICE: 'Marktpreis',
        PREDUMP: 'Vorentladung',
        WALLBOX_SUPPORT: 'Wallbox-Unterstützung',
        MANUAL: 'Manuell',
        STORAGE_ACTIVE: 'Speicher aktiv',
        E3DC_AUTO: 'E3DC Auto',
        E3DC_AUTONOM: 'E3DC autonom',
        PARALLEL_WB_AUTO: 'Wallbox-Automatik'
    };
    return labels[name] || name;
}

function ruleCalmPatternLabel(name) {
    const labels = {
        protection_curve_protection: 'Schutzpfad → Ladekurve → Schutzpfad',
        curve_protection_curve: 'Ladekurve → Schutzpfad → Ladekurve'
    };
    return labels[name] || name;
}

function ruleCalmSelectedServices() {
    return Array.from(document.querySelectorAll('.rule-calm-service:checked')).map(item => item.value);
}

function updateRuleCalmSourceControls() {
    const source = document.getElementById('ruleCalmSourceSelect')?.value || 'current';
    document.getElementById('ruleCalmUploadRow')?.classList.toggle('active', source === 'upload');
    const scope = document.getElementById('ruleCalmScopeSelect');
    if (scope) scope.disabled = source !== 'current';
}

function openRuleCalmAnalysis() {
    document.getElementById('ruleCalmAnalysisBox')?.scrollIntoView({behavior: 'smooth', block: 'start'});
    runRuleCalmAnalysis();
}

function ruleCalmActionClass(action) {
    return String(action || 'unknown').toLowerCase().replace(/[^a-z0-9_-]+/g, '');
}

function ruleCalmShortTime(value) {
    const text = String(value || '');
    if (!text) return '';
    return text.length > 11 ? text.slice(11, 19) : text;
}

function ruleCalmRecordRange(data) {
    const summary = data.record_summary || {};
    if (summary.first_time && summary.last_time) return `${summary.first_time} bis ${summary.last_time}`;
    const timeline = data.timeline_summary || {};
    if (timeline.first_time && timeline.last_time) return `${timeline.first_time} bis ${timeline.last_time}`;
    return 'nicht ermittelbar';
}

function ruleCalmScopeDetail(data) {
    const context = data.scope_context || {};
    const parts = [];
    if (data.scope_label || context.scope_label) parts.push(data.scope_label || context.scope_label);
    if (context.cutoff_time) parts.push(`Schnitt ${context.cutoff_time}`);
    if ((data.scope || context.scope) === 'latest') parts.push('historisch; kann mehrere Prozessgenerationen enthalten');
    return parts.join(' · ');
}

function renderRuleCalmTimeline(data) {
    const timeline = Array.isArray(data.timeline) ? data.timeline.slice(-80) : [];
    if (!timeline.length) {
        return '<div class="text-secondary small mt-1">Keine Wechsel für die Zeitachse gefunden. Der geprüfte Record-Zeitraum steht in der Zusammenfassung.</div>';
    }
    const rows = timeline.map(item => {
        const pattern = item.pattern || '';
        return `
            <div class="rule-calm-event ${item.alert ? 'alert' : ''}">
                <div class="text-secondary rule-calm-time">${esc(ruleCalmShortTime(item.time) || item.time || '')}</div>
                <div class="rule-calm-lane">${esc(ruleCalmCheckLabel(item.lane || ''))}</div>
                <div><span class="rule-calm-action ${esc(ruleCalmActionClass(item.action))}">${esc(ruleCalmActionLabel(item.action) || '-')}</span></div>
                <div class="rule-calm-detail">
                    ${item.actor ? `<span class="small-code">${esc(ruleCalmActorLabel(item.actor))}</span>` : ''}
                    ${pattern ? `<div class="text-secondary mt-1">${esc(ruleCalmPatternLabel(pattern))}</div>` : ''}
                    ${item.alert ? '<span class="badge text-bg-warning mt-1">Muster</span>' : ''}
                    ${!item.alert && item.lane === 'storage_live_plausibility' && pattern ? '<span class="badge text-bg-info mt-1">Datenhinweis</span>' : ''}
                </div>
            </div>`;
    }).join('');
    return `<div class="text-secondary small mt-1">Die Zeitachse zeigt echte Wechsel und Schutzereignisse, nicht jeden Record.</div><div class="rule-calm-timeline">${rows}</div>`;
}

function renderRuleCalmViolations(violations, emptyText = 'Keine auffälligen Muster erkannt.') {
    if (!violations.length) {
        return `<div class="result-tile"><span class="ok">${esc(emptyText)}</span></div>`;
    }
    const rows = violations.map(item => {
        const samples = Array.isArray(item.events)
            ? item.events.slice(0, 3).map(event => `${ruleCalmShortTime(event.time)} ${ruleCalmActionLabel(event.action) || ''}`.trim()).filter(Boolean).join(' → ')
            : '';
        const time = item.first_time && item.last_time && item.first_time !== item.last_time
            ? `${item.first_time} – ${item.last_time}`
            : (item.first_time || item.last_time || samples || '-');
        return `<tr>
            <td data-label="Bereich">${esc(ruleCalmCheckLabel(item.check))}</td>
            <td data-label="Akteur">${item.actor ? `<span class="small-code">${esc(ruleCalmActorLabel(item.actor))}</span>` : '-'}</td>
            <td data-label="Muster">${esc(`${ruleCalmPatternLabel(item.type) || 'Muster'}${item.count ? ` (${item.count}x)` : ''}`)}</td>
            <td data-label="Zeitpunkt">${esc(time)}</td>
            <td data-label="Fenster">${item.age_s !== null && item.age_s !== undefined ? esc(Math.round(Number(item.age_s)) + 's') : '-'}</td>
        </tr>`;
    }).join('');
    return `<div class="rule-calm-table-wrap"><table class="rule-calm-table">
        <thead><tr><th>Bereich</th><th>Akteur</th><th>Muster</th><th>Zeitpunkt</th><th>Fenster</th></tr></thead>
        <tbody>${rows}</tbody>
    </table></div>`;
}

function renderRuleCalmAnalysis(data) {
    if (!data || data.success === false) {
        return `<div class="bad"><strong>Keine belastbare Regelruhe-Aussage.</strong><div class="mt-1">${esc(data && (data.error || data.message) || 'Keine Antwort erhalten.')}</div></div>`;
    }
    const records = data.records || {};
    const events = data.events || {};
    const checks = data.checks || {};
    const violations = Array.isArray(data.violations) ? data.violations : [];
    const dataQualityFindings = Array.isArray(data.data_quality_findings) ? data.data_quality_findings : [];
    const controlStatus = String(data.control_status || data.status || 'UNKNOWN').toUpperCase();
    const dataQualityStatus = String(data.data_quality_status || 'NOT_ANALYZED').toUpperCase();
    const completeness = String(data.completeness || 'LEGACY').toUpperCase();
    const partial = completeness === 'PARTIAL';
    const legacy = completeness === 'LEGACY';
    const typedEvidenceLimit = controlStatus === 'EVIDENCE_LIMIT' || dataQualityStatus === 'EVIDENCE_LIMIT';
    const evidenceLimit = partial || legacy || typedEvidenceLimit;
    const missingServices = Array.isArray(data.missing_services) ? data.missing_services : [];
    const analyzedServices = Array.isArray(data.analyzed_services) ? data.analyzed_services : [];
    const recordRange = ruleCalmRecordRange(data);
    const scopeDetail = ruleCalmScopeDetail(data);
    const renderCheckRow = ([name, check]) => {
        const ok = check && check.ok !== false;
        const counts = check && check.counts
            ? Object.entries(check.counts).filter(([, value]) => Number(value || 0) > 0)
            : [];
        const countText = counts.length ? counts.map(([key, value]) => `${ruleCalmPatternLabel(key)}: ${value}`).join(', ') : 'keine Muster';
        return `<li>${boolBadge(ok, 'OK', 'auffällig', true)} <strong>${esc(ruleCalmCheckLabel(name))}</strong><div class="text-secondary small">${esc(countText)}</div></li>`;
    };
    const checkRows = Object.entries(checks)
        .filter(([name]) => name !== 'storage_live_plausibility')
        .map(renderCheckRow)
        .join('');
    const dataQualityCheckRow = checks.storage_live_plausibility
        ? renderCheckRow(['storage_live_plausibility', checks.storage_live_plausibility])
        : '<li>Keine Speicher-Datenqualität ausgewertet.</li>';
    const historical = data.scope === 'latest';
    const laneMeaning = controlStatus === 'FAIL'
        ? 'Im Entscheidungs-/Ausgangspfad wurde ein belegtes Ping-Pong, Veto oder ein Pfadkonflikt erkannt.'
        : controlStatus === 'EVIDENCE_LIMIT'
        ? 'Für eine belastbare Regelruhe-Aussage fehlt in mindestens einem Speicher-Record die typisierte Execution-/Ausgangssignatur.'
        : 'Entscheidungsweg und tatsächlicher Aktor-/Ausgang blieben ohne belegtes Ping-Pong.';
    const dataQualityMeaning = dataQualityStatus === 'FAIL'
        ? 'Wiederholte oder anhaltende Messwertschutz-Guards machen die Datenqualität auffällig.'
        : dataQualityStatus === 'HINT'
        ? 'Ein einzelner oder kurzer Messwertschutz-Guard ist als Datenqualitätshinweis eingeordnet.'
        : dataQualityStatus === 'PASS'
        ? 'Im ausgewerteten Speicherverlauf liegt kein Messwertqualitätsbefund vor.'
        : dataQualityStatus === 'EVIDENCE_LIMIT'
        ? 'Für eine belastbare Datenqualitätsaussage fehlt in mindestens einem Speicher-Record die typisierte Live-Evidenz.'
        : 'Die Speicher-Datenqualität wurde nicht separat ausgewertet.';
    const meaning = legacy
        ? 'Diese gespeicherte Auswertung stammt aus dem alten Public-v2-Vertrag. Ihr damaliger PASS-/FAIL-Status enthält keine belastbare Domänen-Vollständigkeit und wird deshalb nur als EVIDENCE_LIMIT angezeigt.'
        : partial
        ? `Die Auswertung ist unvollständig: ${missingServices.map(ruleCalmServiceLabel).join(', ')} hatte keine auswertbaren Records. Die getrennten Befunde gelten nur für ${analyzedServices.map(ruleCalmServiceLabel).join(', ')}. ${laneMeaning} ${dataQualityMeaning}`
        : `${historical ? 'Historischer Befund: ' : ''}${laneMeaning} ${dataQualityMeaning}${historical ? ' Das ist kein Beleg für einen Fehler des aktuellen Prozesses.' : ''}`;
    const effectiveGaps = data.effective_min_gap_s || {};
    const ownerGap = Math.max(
        Number(effectiveGaps.storage_contract_owner || 0),
        Number(effectiveGaps.storage_owner || 0),
        Number(effectiveGaps.storage_state || 0)
    );
    const gapText = `Musterabstand ${data.min_gap_s || 180}s${ownerGap > Number(data.min_gap_s || 180) ? ` · Contract/Owner/State ${ownerGap}s` : ''}`;
    const title = historical ? 'Historische Regelruhe-Diagnose' : 'Aktuelle Regelruhe-Diagnose';
    return `
        <div class="result-title"><i class="fas fa-wave-square ${controlStatus === 'PASS' ? 'ok' : 'warn'}"></i>${esc(title)} <span>Regelruhe ${ruleCalmStatusBadge(controlStatus)}</span> <span>Datenqualität ${ruleCalmDataQualityBadge(dataQualityStatus)}</span>${legacy ? ' <span class="badge text-bg-warning">LEGACY / EVIDENCE_LIMIT</span>' : (partial ? ' <span class="badge text-bg-warning">TEILWEISE / EVIDENCE_LIMIT</span>' : '')}${historical ? ' <span class="badge text-bg-secondary">historisch</span>' : ''}</div>
        <div class="text-secondary small">${esc(data.source_label || 'Entscheidungsverlauf')} · ${scopeDetail ? esc(scopeDetail) + ' · ' : ''}Geprüfte Records ${esc(recordRange)} · ${esc(gapText)}</div>
        <div class="result-grid">
            <div class="result-tile"><strong>Speicher</strong>${esc(records.storage ?? 0)} Records<div class="text-secondary mt-1">${esc(events.storage_contract_owner ?? 0)} Contract · ${esc(events.storage_owner ?? 0)} Owner · ${esc(events.storage_execution_class ?? 0)} Ausführung · ${esc(events.storage_state ?? 0)} State</div><div class="text-secondary mt-1">${esc(events.storage_value_update ?? 0)} Werte · ${esc(events.storage ?? 0)} RSCP-Commands · ${esc(events.storage_live_plausibility ?? 0)} Messwertschutz</div></div>
            <div class="result-tile"><strong>Wallbox</strong>${esc(records.wallbox ?? 0)} Records<div class="text-secondary mt-1">${esc(events.wallbox_start_stop ?? 0)} Start/Stop · ${esc(events.wallbox_phase ?? 0)} Phasen-Commands</div></div>
            <div class="result-tile"><strong>Wärmepumpe</strong>${esc(records.heatpump ?? 0)} Records<div class="text-secondary mt-1">${esc(events.heatpump ?? 0)} Entscheidungswechsel; OBS_* bleibt beobachtet</div></div>
            <div class="result-tile"><strong>EMS</strong>${esc(records.ems ?? 0)} Records<div class="text-secondary mt-1">${esc(events.ems_decision ?? 0)} kanonische Entscheidungen</div></div>
            <div class="result-tile"><strong>Prüfzeitraum</strong>${esc(recordRange)}<div class="text-secondary mt-1">aus den geprüften Records</div></div>
            <div class="result-tile"><strong>Prozessgrenze</strong>${historical ? 'prozessübergreifende Historie' : esc((data.scope_context || {}).cutoff_time || 'nicht ermittelbar')}<div class="text-secondary mt-1">${historical ? 'kein Beleg für den aktuellen Prozess' : 'ältere Records ausgeschlossen'}</div></div>
            <div class="result-tile"><strong>Regelruhe</strong>${ruleCalmStatusBadge(controlStatus)}<div class="text-secondary mt-1">${violations.length ? `${esc(violations.length)} belegte Regelmuster` : 'kein belegtes Execution-/Ausgangs-Ping-Pong'}</div></div>
            <div class="result-tile"><strong>Datenqualität</strong>${ruleCalmDataQualityBadge(dataQualityStatus)}<div class="text-secondary mt-1">${dataQualityFindings.length ? `${esc(dataQualityFindings.length)} Befund` : 'kein separater Befund'}</div></div>
        </div>
        <div class="result-tile mt-2"><strong>Einordnung</strong>${esc(meaning)}<div class="text-secondary mt-1">${esc(data.privacy_note || 'Read-only Diagnose ohne Hardwarezugriff.')}</div></div>
        ${evidenceLimit ? `<div class="result-tile warn mt-2"><strong>EVIDENCE_LIMIT</strong>${legacy ? 'Historischer Public-Vertrag ohne Evidenz-Lanes' : (partial ? `Fehlende Domänen: ${esc(missingServices.map(ruleCalmServiceLabel).join(', '))}` : 'Fehlende typisierte Live- oder Execution-/Ausgangsevidenz')}<div class="text-secondary mt-1">Diese Auswertung erlaubt keine vollständige Grün-Aussage; echte Veto-/Pfadkonflikte und belegte Ausgangswechsel bleiben davon unabhängig sichtbar.</div></div>` : ''}
        <div class="mt-2"><strong>Regelpfade und Ausgänge</strong><ul class="result-list">${checkRows || '<li>Keine Prüfbereiche gefunden.</li>'}</ul></div>
        <div class="mt-2"><strong>Datenqualität</strong><ul class="result-list">${dataQualityCheckRow}</ul></div>
        <div class="mt-2"><strong>Datenqualitätsbefunde</strong>${renderRuleCalmViolations(dataQualityFindings, 'Kein Messwertqualitätsbefund erkannt.')}</div>
        <div class="mt-2"><strong>Regelauffälligkeiten mit Zeitpunkt</strong>${renderRuleCalmViolations(violations, 'Keine belegten Regelmuster erkannt.')}</div>
        <div class="mt-2"><strong>Zeitachse</strong>${renderRuleCalmTimeline(data)}</div>
        <details class="mt-2">
            <summary class="text-secondary small">Forum-Zusammenfassung anzeigen</summary>
            <div id="ruleCalmForumText" class="raw-json">${esc(data.forum_summary || '')}</div>
            <button class="btn btn-sm btn-outline-info mt-2" onclick="copyRuleCalmForumSummary()"><i class="fas fa-copy me-1"></i>Forum-Text kopieren</button>
        </details>`;
}

async function copyRuleCalmForumSummary() {
    const text = document.getElementById('ruleCalmForumText')?.textContent || '';
    if (!text) return;
    const fallbackCopy = () => {
        const area = document.createElement('textarea');
        area.value = text;
        area.setAttribute('readonly', 'readonly');
        area.style.position = 'fixed';
        area.style.left = '-9999px';
        document.body.appendChild(area);
        area.select();
        let ok = false;
        try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
        document.body.removeChild(area);
        return ok;
    };
    try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
            return;
        }
        fallbackCopy();
    } catch (err) {
        fallbackCopy();
    }
}

async function runRuleCalmAnalysis() {
    const box = document.getElementById('ruleCalmAnalysisResult');
    const source = document.getElementById('ruleCalmSourceSelect')?.value || 'current';
    const services = ruleCalmSelectedServices();
    if (source !== 'history' && !services.length) {
        if (box) box.innerHTML = '<div class="warn">Bitte mindestens einen Dienst auswählen.</div>';
        return;
    }
    const minGap = document.getElementById('ruleCalmMinGapSelect')?.value || '180';
    const scope = document.getElementById('ruleCalmScopeSelect')?.value || 'manager_restart';
    if (box) box.innerHTML = '<div class="text-secondary"><i class="fas fa-spinner fa-spin me-1"></i>Entscheidungsverläufe werden read-only ausgewertet...</div>';
    try {
        let data;
        if (source === 'history') {
            data = await loadJson('rule_calm_analysis.php?action=history');
        } else if (source === 'upload') {
            const input = document.getElementById('diagnoseZipInput');
            if (!input || !input.files || !input.files.length) throw new Error('Bitte zuerst ein Diagnose-ZIP auswählen.');
            const form = new FormData();
            form.append('csrf_token', installCenterCsrfToken);
            form.append('diagnose_zip', input.files[0]);
            form.append('min_gap_s', minGap);
            form.append('limit', '1200');
            services.forEach(service => form.append('service[]', service));
            const res = await fetch('rule_calm_analysis.php?action=analyze_upload', {method: 'POST', body: form, cache: 'no-store'});
            data = await res.json();
        } else {
            const params = new URLSearchParams({action: 'analyze', min_gap_s: minGap, limit: '1200', scope});
            services.forEach(service => params.append('service[]', service));
            data = await loadJson(`rule_calm_analysis.php?${params.toString()}`);
        }
        if (box) box.innerHTML = renderRuleCalmAnalysis(data);
    } catch (err) {
        if (box) box.innerHTML = `<div class="bad"><strong>Regelruhe-Diagnose fehlgeschlagen.</strong><div class="mt-1">${esc(err.message || err)}</div></div>`;
    }
}

function renderLogPreview(logPreview) {
    if (!logPreview || !logPreview.path) return '';
    if (logPreview.error) {
        return `<div class="mt-2"><strong>Log-Vorschau</strong><div class="result-tile warn mt-1">${esc(logPreview.error)}</div></div>`;
    }
    if (!logPreview.exists) {
        return `<div class="mt-2"><strong>Log-Vorschau</strong><div class="result-tile text-secondary mt-1">Noch keine Logdatei vorhanden: <span class="small-code">${esc(logPreview.path)}</span></div></div>`;
    }
    const lines = (logPreview.lines || []).map(line => esc(line)).join('\n');
    return `
        <div class="mt-2">
            <strong>Letzte Logzeilen</strong>
            <div class="text-secondary small"><span class="small-code">${esc(logPreview.path)}</span></div>
            <pre class="raw-json">${lines || 'Logdatei ist leer.'}</pre>
        </div>
    `;
}

function renderJournalPreview(journalPreview) {
    if (!journalPreview || !journalPreview.unit) return '';
    if (journalPreview.error) {
        return `<div class="mt-2"><strong>Journal-Vorschau</strong><div class="result-tile warn mt-1">${esc(journalPreview.error)}</div></div>`;
    }
    if (!journalPreview.available) return '';
    const lines = (journalPreview.lines || []).map(line => esc(line)).join('\n');
    return `
        <div class="mt-2">
            <strong>Letzte Journal-Zeilen</strong>
            <div class="text-secondary small"><span class="small-code">${esc(journalPreview.unit)}</span></div>
            <pre class="raw-json">${lines || 'Keine Journal-Zeilen gefunden.'}</pre>
        </div>
    `;
}

function renderDiagnosisSummary(block) {
    if (!block || !block.module) return '';
    const module = block.module || {};
    const systemd = block.systemd || {};
    const alive = block.alive || {};
    const config = block.config || {};
    const logPreview = block.log || {};
    const journalPreview = block.journal || {};
    const missing = config.missing_keys || [];
    const missingDisplay = configDisplayList(config, 'missing_labels', 'missing_keys');
    let level = 'ok';
    let title = 'Sieht gesund aus';
    let message = 'Dienst, Konfiguration und Daten wirken plausibel.';
    let next = 'Keine Aktion nötig. Bei Auffälligkeiten können Diagnose und Log-Vorschau geprüft werden.';

    if (config.ok === false || missing.length) {
        level = 'warn';
        title = 'Konfiguration unvollständig';
        message = `${esc(module.display_name || module.key)} kann erst sauber laufen, wenn die Pflichtwerte gesetzt sind.`;
        next = `Config öffnen und fehlende Werte prüfen: ${esc(missingDisplay.join(', '))}`;
    } else if (!systemd.exists && !block.docker) {
        level = 'bad';
        title = 'Dienst ist nicht installiert';
        message = 'Die systemd-Unit fehlt. Das Modul kann deshalb nicht automatisch starten.';
        next = 'Installationsdetails prüfen oder den sicheren Job-Test vorbereiten.';
    } else if (systemd.exists && !systemd.active && !block.docker) {
        level = 'warn';
        title = 'Dienst ist installiert, aber inaktiv';
        message = 'Die Service-Datei ist vorhanden, der Dienst läuft aber gerade nicht.';
        next = 'Starten oder zuerst die Log-/Journal-Vorschau prüfen, wenn der Dienst wiederholt stoppt.';
    } else if (alive.path && !alive.fresh) {
        level = 'warn';
        title = alive.exists ? 'Daten sind veraltet' : 'Noch kein Datensignal';
        message = alive.exists
            ? `Die Alive-Datei ist ${esc(alive.age_s)}s alt und damit älter als erwartet.`
            : 'Die erwartete Alive-Datei wurde noch nicht geschrieben.';
        next = 'Dienststatus, Konfiguration und letzte Log-/Journal-Zeilen prüfen.';
    } else if ((logPreview.error || journalPreview.error) && !logPreview.lines?.length && !journalPreview.lines?.length) {
        level = 'warn';
        title = 'Diagnose-Hinweis';
        message = 'Der Dienst wirkt aktiv, aber Log- oder Journal-Vorschau konnte nicht vollständig gelesen werden.';
        next = 'Rechteprüfung ausführen, falls die Vorschau dauerhaft leer bleibt.';
    }

    const icon = level === 'ok' ? 'fa-circle-check ok' : (level === 'bad' ? 'fa-circle-xmark bad' : 'fa-triangle-exclamation warn');
    return `
        <div class="result-tile mt-2">
            <div class="result-title mb-1"><i class="fas ${icon}"></i>${esc(title)}</div>
            <div>${message}</div>
            <div class="text-secondary small mt-2"><strong>Nächster Schritt:</strong> ${next}</div>
        </div>
    `;
}

function renderProgressSteps(steps) {
    if (!Array.isArray(steps) || !steps.length) return '';
    const iconFor = state => {
        if (state === 'done') return '<i class="fas fa-check-circle ok me-1"></i>';
        if (state === 'running') return '<i class="fas fa-spinner fa-spin warn me-1"></i>';
        if (state === 'blocked') return '<i class="fas fa-lock warn me-1"></i>';
        if (state === 'error') return '<i class="fas fa-circle-xmark bad me-1"></i>';
        return '<i class="far fa-circle text-secondary me-1"></i>';
    };
    return `<div class="mt-2"><strong>Job-Fortschritt</strong><ul class="result-list">${
        steps.map(step => `<li>${iconFor(step.state)}${esc(step.label || '')}</li>`).join('')
    }</ul></div>`;
}

function showJobModal(title, subtitle, bodyHtml = '', wide = false) {
    document.getElementById('jobModalTitle').innerHTML = title;
    document.getElementById('jobModalSubtitle').innerHTML = subtitle;
    document.getElementById('jobModalBody').innerHTML = bodyHtml || '<div class="text-secondary">Bereit.</div>';
    const panel = document.querySelector('#jobModal .job-modal-panel');
    if (panel) panel.classList.toggle('wide', Boolean(wide));
    document.getElementById('jobModal').classList.remove('d-none');
}

function hideJobModal() {
    document.getElementById('jobModal').classList.add('d-none');
    const panel = document.querySelector('#jobModal .job-modal-panel');
    if (panel) panel.classList.remove('wide');
    if (jobModalRefreshTimer) {
        window.clearInterval(jobModalRefreshTimer);
        jobModalRefreshTimer = null;
    }
}

function renderJobModalStatus(data) {
    const last = data && data.last_job ? data.last_job : {};
    const job = last.job || {};
    const result = last.result || {};
    const state = last.state || 'kein Job';
    const running = state === 'running' || Boolean(data && data.lock_active);
    const ok = state === 'done' || state === 'kein Job';
    const message = result.message || result.summary || result.error || 'Noch keine Ergebnis-Meldung.';
    const block = firstObject(result.install_dry_run);
    const installPlanHtml = block && block.install_plan ? renderInstallPlan(block.install_plan) : '';
    const readinessHtml = block && block.readiness ? renderReadiness(block.readiness) : '';
    return `
        <div class="job-progress-box">
            <div class="result-title"><i class="fas ${running ? 'fa-spinner fa-spin warn' : (ok ? 'fa-circle-check ok' : 'fa-triangle-exclamation warn')}"></i>${esc(jobStateLabel(state))}</div>
            <div class="text-secondary small">${esc(actionLabel(job.action))}${job.module ? ` / ${esc(job.module)}` : ''}</div>
            <div class="mt-2">${esc(message)}</div>
            ${renderProgressSteps(last.progress_steps || [])}
        </div>
        ${readinessHtml}
        ${installPlanHtml}
        <div class="text-secondary small mt-2">Dieser Test nutzt denselben Ramdisk-Jobpfad wie spätere echte Installationen, bleibt aber read-only.</div>
    `;
}

async function updateJobModalFromStatus() {
    try {
        const data = await loadJson('install_center.php?action=job_status');
        document.getElementById('jobModalBody').innerHTML = renderJobModalStatus(data);
        return data;
    } catch (err) {
        document.getElementById('jobModalBody').innerHTML = `<div class="bad">Job-Status konnte nicht gelesen werden: ${esc(err.message || err)}</div>`;
        return null;
    }
}

function renderWriteGatePreview(data) {
    if (!data || data.success === false) {
        return `<div class="bad">Freigabe-Check konnte nicht gelesen werden: ${esc(data && (data.error || data.message) || 'keine Antwort')}</div>`;
    }
    const checks = data.checks || [];
    const hardBlockers = data.hard_blocker_count || 0;
    const ready = Boolean(data.privileged_installer_web_enabled && data.ready_for_manual_enable);
    const rows = checks.map(item => {
        const ok = Boolean(item.ok);
        const hard = Boolean(item.hard);
        const icon = ok ? '<i class="fas fa-check-circle ok me-1"></i>' : (hard ? '<i class="fas fa-circle-xmark bad me-1"></i>' : '<i class="fas fa-triangle-exclamation warn me-1"></i>');
        const detail = item.issue || item.status || item.path || '';
        return `<li>${icon}<strong>${esc(item.label || 'Check')}</strong>${detail ? ` <span class="text-secondary">- ${esc(detail)}</span>` : ''}</li>`;
    }).join('');
    return `
        <div class="job-progress-box mt-3">
            <strong><i class="fas fa-user-shield text-info me-1"></i>Aktueller Freigabe-Check</strong>
            <div class="result-grid mt-2">
                <div class="result-tile"><strong>Schreibmodus</strong>${boolBadge(Boolean(data.write_actions_enabled), 'frei', 'gesperrt', true)}</div>
                <div class="result-tile"><strong>System bereit</strong>${boolBadge(ready, 'vorbereitet', 'nicht bereit', true)}</div>
                <div class="result-tile"><strong>Harte Blocker</strong>${boolBadge(hardBlockers === 0, 'keine', hardBlockers + ' Blocker', true)}</div>
            </div>
            ${rows ? `<ul class="result-list">${rows}</ul>` : ''}
            <div class="text-secondary small mt-2">${esc(data.next_step || 'Echte Schreibjobs bleiben bis zur bewussten Freigabe gesperrt.')}</div>
        </div>
    `;
}

async function showBlockedInstall(moduleKey, displayName) {
    showJobModal(
        '<i class="fas fa-lock text-warning me-2"></i>Installation noch gesperrt',
        `${esc(displayName || moduleKey)}: Sicherheitsstufe read-only`,
        `
            <div class="alert alert-warning bg-warning bg-opacity-10 border-warning text-warning mb-3">
                Echte Modulinstallation ist bewusst noch nicht freigeschaltet. So kann die Seite auf Testsystemen geprüft werden, ohne Dienste, Config oder Historie zu verändern.
            </div>
            <div class="job-progress-box">
                <strong>Freigabereihenfolge</strong>
                <ul class="result-list">
                    <li><i class="fas fa-check-circle ok me-1"></i>Diagnose und Dry-Run lesen nur.</li>
                    <li><i class="fas fa-check-circle ok me-1"></i>Job-Test prüft den späteren Ramdisk-Auftrag.</li>
                    <li><i class="far fa-circle text-secondary me-1"></i>Schreibmodus wird erst nach Wrapper-/sudoers-Freigabe aktiviert.</li>
                    <li><i class="far fa-circle text-secondary me-1"></i>Erste echte Installation nur mit Backup auf einem Testsystem.</li>
                </ul>
            </div>
            <div class="mt-3">
                <button class="btn btn-sm btn-outline-primary" onclick="runModuleJob('${esc(moduleKey)}','install_module_dry_run')"><i class="fas fa-clipboard-check me-1"></i>Stattdessen Job-Test starten</button>
            </div>
            <div id="writeGatePreview" class="mt-3 text-secondary small">
                <i class="fas fa-spinner fa-spin me-1"></i>Freigabe-Check wird gelesen...
            </div>
        `
    );
    try {
        const data = await loadJson('install_center.php?action=write_readiness');
        const box = document.getElementById('writeGatePreview');
        if (box) box.innerHTML = renderWriteGatePreview(data);
    } catch (err) {
        const box = document.getElementById('writeGatePreview');
        if (box) box.innerHTML = `<div class="bad">Freigabe-Check konnte nicht geladen werden: ${esc(err.message || err)}</div>`;
    }
}

function jobStateLabel(state) {
    const labels = {
        running: 'läuft',
        done: 'fertig',
        error: 'Fehler',
        blocked: 'blockiert',
        known: 'bekannt'
    };
    return labels[state] || state || 'kein Job';
}

function actionLabel(action) {
    const labels = {
        install_module_dry_run: 'Installations-Dry-Run',
        dry_run: 'Modul-Dry-Run',
        run_diagnosis: 'Diagnose',
        diagnosis: 'Diagnose',
        permissions_check: 'Nur Rechte prüfen',
        repair_permissions_dry_run: 'Reparatur-Dry-Run',
        repair_permissions: 'Systemreparatur',
        install_module: 'Modulinstallation',
        remove_module: 'Modul-Rückbau',
        write_readiness: 'Freigabe-Check',
        write_permission_plan: 'Freigabe-Plan',
        backup_plan: 'Backup-Plan',
        job_status: 'Job-Status'
    };
    return labels[action] || action || 'kein Job';
}

function renderInstallerStatus(data) {
    if (!data || data.success === false) {
        return `<div class="installer-status-head"><strong class="bad"><i class="fas fa-triangle-exclamation me-1"></i>Installer-Status nicht lesbar</strong></div><div class="text-secondary small mt-2">${esc(data && (data.error || data.message) || 'Keine Antwort')}</div>`;
    }
    const writeEnabled = Boolean(data.write_actions_enabled);
    const lastJob = data.last_job || {};
    const hasLastJob = Object.keys(lastJob).length > 0;
    const job = lastJob.job || {};
    const result = lastJob.result || {};
    const jobState = lastJob.state || (hasLastJob ? 'known' : 'kein Job');
    const running = jobState === 'running' || Boolean(data.lock_active);
    const jobText = actionLabel(job.action);
    const resultText = result.message || result.summary || result.error || lastJob.updated_at || 'noch kein Jobstatus';
    return `
        <div class="installer-status-head">
            <div>
                <strong><i class="fas fa-gauge-high text-info me-1"></i>Installer-Status</strong>
                <div class="text-secondary small">${esc(data.message || 'Bereit.')} ${running ? '<span class="ms-1"><i class="fas fa-spinner fa-spin"></i> Job läuft...</span>' : ''}</div>
                <div class="text-secondary small">Letzter Job wird nur durch Job-Test oder spätere echte Installer-Jobs aktualisiert.</div>
            </div>
            <div class="installer-status-badges">
                ${boolBadge(writeEnabled, 'Schreibmodus frei', 'Schreibmodus gesperrt', true)}
                ${boolBadge(Boolean(data.docker), 'Docker', 'Bare-Metal', true)}
                ${boolBadge(!data.lock_active, 'kein Lock', 'Job-Lock aktiv', true)}
            </div>
        </div>
        <div class="installer-status-meta">
            <div><strong>Installationspfad</strong><br><span class="small-code">${esc(data.install_root || '')}</span></div>
            <div><strong>Letzter Job</strong><br>${esc(jobStateLabel(jobState))}${job.action ? `: ${esc(jobText)}` : ''}${job.module ? ` / ${esc(job.module)}` : ''}</div>
            <div><strong>Letzte Meldung</strong><br>${esc(resultText)}</div>
        </div>
    `;
}

function renderInstallPlan(plan) {
    if (!plan || typeof plan !== 'object') return '';
    const list = items => (items || []).map(item => `<li>${esc(item)}</li>`).join('');
    const affected = plan.affected || {};
    const affectedRows = Object.entries(affected)
        .filter(([, value]) => value !== null && value !== undefined && value !== '' && (!Array.isArray(value) || value.length))
        .map(([key, value]) => `<li><strong>${esc(key)}</strong>: <span class="small-code">${esc(Array.isArray(value) ? value.join(', ') : value)}</span></li>`)
        .join('');
    return `
        <div class="mt-3">
            <strong><i class="fas fa-route text-info me-1"></i>Installationsplan</strong>
            <div class="result-grid">
                <div class="result-tile"><strong>Vorher</strong><ul class="result-list">${list(plan.before)}</ul></div>
                <div class="result-tile"><strong>Würde ausführen</strong><ul class="result-list">${list(plan.would_do)}</ul></div>
                <div class="result-tile"><strong>Danach erwartet</strong><ul class="result-list">${list(plan.expected_after)}</ul></div>
                <div class="result-tile"><strong>Rückweg</strong><ul class="result-list">${list(plan.rollback)}</ul></div>
            </div>
            ${plan.safety_checks ? `<div class="mt-2"><strong>Sicherheitschecks</strong><ul class="result-list">${list(plan.safety_checks)}</ul></div>` : ''}
            ${affectedRows ? `<details class="mt-2"><summary class="text-secondary small">Betroffene Dateien und Werte</summary><ul class="result-list">${affectedRows}</ul></details>` : ''}
        </div>
    `;
}

function renderReadiness(readiness) {
    if (!readiness || typeof readiness !== 'object') return '';
    const state = readiness.state || 'unknown';
    const icon = {
        ready: 'fa-circle-check ok',
        installed: 'fa-circle-check ok',
        blocked: 'fa-triangle-exclamation warn',
        docker_pending: 'fa-box warn'
    }[state] || 'fa-circle-info text-info';
    const reasons = (readiness.reasons || []).map(reason => `<li>${esc(reason)}</li>`).join('');
    return `
        <div class="mt-3 result-tile">
            <div class="result-title mb-1"><i class="fas ${icon}"></i>${esc(readiness.label || 'Freigabeprüfung')}</div>
            <div>${esc(readiness.message || '')}</div>
            <div class="mt-2">${boolBadge(Boolean(readiness.can_install_when_writes_enabled), 'mit Schreibfreigabe installierbar', 'nicht installierbar', true)}</div>
            ${reasons ? `<ul class="result-list">${reasons}</ul>` : ''}
        </div>
    `;
}

function renderReadinessOverview(data) {
    const blocks = data.install_dry_run || {};
    const entries = Object.entries(blocks);
    if (!entries.length) return `<div class="bad">Keine Moduldaten erhalten.</div>${renderRawDetails(data)}`;
    const labels = {
        ready: 'bereit',
        installed: 'fertig',
        blocked: 'blockiert',
        docker_pending: 'Docker',
        unknown: 'unklar'
    };
    const icons = {
        ready: 'fa-circle-check ok',
        installed: 'fa-circle-check ok',
        blocked: 'fa-circle-xmark bad',
        docker_pending: 'fa-box warn',
        unknown: 'fa-circle-info text-info'
    };
    const badgeClasses = {
        ready: 'text-bg-success',
        installed: 'text-bg-primary',
        blocked: 'text-bg-danger',
        docker_pending: 'text-bg-warning',
        unknown: 'text-bg-secondary'
    };
    const counts = entries.reduce((acc, [, block]) => {
        const state = (block.readiness && block.readiness.state) || 'unknown';
        acc[state] = (acc[state] || 0) + 1;
        return acc;
    }, {});
    const countBadges = Object.entries(labels)
        .filter(([state]) => counts[state])
        .map(([state, label]) => `<span class="badge ${badgeClasses[state] || badgeClasses.unknown} me-1 mb-1">${esc(label)}: ${esc(counts[state])}</span>`)
        .join('');
    const cards = entries.map(([key, block]) => {
        const module = block.module || {};
        const readiness = block.readiness || {};
        const state = readiness.state || 'unknown';
        const canInstall = Boolean(readiness.can_install_when_writes_enabled);
        const reasons = (readiness.reasons || block.blocked_reasons || [])
            .map(reason => `<li>${esc(reason)}</li>`)
            .join('');
        const service = (block.service && (block.service.unit || block.service.service_unit)) || module.service_unit || '';
        const installBadge = (() => {
            if (state === 'installed') return '<span class="badge text-bg-primary ms-1">fertig eingerichtet</span>';
            if (state === 'blocked') return '<span class="badge text-bg-danger ms-1">erst Blocker beheben</span>';
            if (state === 'docker_pending') return '<span class="badge text-bg-warning ms-1">Docker-Sonderfall</span>';
            if (block.would_change) return '<span class="badge text-bg-info ms-1">Installation möglich</span>';
            return '<span class="badge text-bg-secondary ms-1">keine Installation nötig</span>';
        })();
        const installAllowedText = (() => {
            if (state === 'installed') return '<span class="badge text-bg-primary">bereits installiert</span>';
            if (state === 'blocked') return '<span class="badge text-bg-danger">nicht installierbar</span>';
            if (state === 'docker_pending') return '<span class="badge text-bg-warning">Docker-Ablauf nötig</span>';
            return boolBadge(canInstall, 'installierbar mit Schreibfreigabe', 'nicht direkt installierbar', true);
        })();
        return `
            <div class="result-tile">
                <div class="d-flex justify-content-between align-items-start gap-2">
                    <div>
                        <strong><i class="fas ${icons[state] || icons.unknown} me-1"></i>${esc(module.display_name || key)}</strong>
                        <div class="text-secondary small">${esc(service)}</div>
                    </div>
                    <span class="badge ${badgeClasses[state] || badgeClasses.unknown}">${esc(labels[state] || state)}</span>
                </div>
                <div class="mt-2">${esc(readiness.message || block.summary || '')}</div>
                <div class="mt-2">
                    ${installAllowedText}
                    ${installBadge}
                </div>
                ${reasons ? `<ul class="result-list">${reasons}</ul>` : ''}
                <button class="btn btn-sm btn-outline-info mt-2" onclick="runModuleAction('${esc(key)}','install_module_dry_run')">
                    <i class="fas fa-magnifying-glass me-1"></i>Details
                </button>
            </div>
        `;
    }).join('');
    return `
        <div class="result-title"><i class="fas fa-traffic-light text-info"></i>Modul-Installierbarkeit</div>
        <div class="text-secondary small">Gesamtprüfung aller Katalogmodule. Es wird nur gelesen; systemd, Config und Historie bleiben unverändert.</div>
        <div class="mt-2">${countBadges}</div>
        <div class="result-grid mt-2">${cards}</div>
        ${renderRawDetails(data)}
    `;
}

function renderModuleResult(data, action) {
    const block = firstObject(data.diagnosis || data.dry_run || data.install_dry_run);
    if (!block) return `<div class="bad">Keine Moduldaten erhalten.</div>${renderRawDetails(data)}`;
    const module = block.module || {};
    const systemd = block.systemd || block.service || {};
    const alive = block.alive || {};
    const config = block.config || {};
    const deps = block.dependencies || {};
    const logPreview = block.log || null;
    const journalPreview = block.journal || null;
    const active = Boolean(systemd.active);
    const installed = Boolean(systemd.exists);
    const fresh = Boolean(alive.fresh || (!alive.path && active));
    const cfgOk = config.ok !== false;
    const blocked = block.blocked_reasons || [];
    const steps = block.planned_steps || [];
    const installPlanHtml = action === 'install_module_dry_run' ? renderInstallPlan(block.install_plan) : '';
    const readinessHtml = action === 'install_module_dry_run' ? renderReadiness(block.readiness) : '';
    const healthy = action === 'diagnosis'
        ? Boolean(block.healthy)
        : installed && cfgOk && (!alive.path || fresh) && blocked.length === 0;
    const icon = healthy ? 'fa-circle-check ok' : (installed ? 'fa-triangle-exclamation warn' : 'fa-circle-xmark bad');
    const titleMap = {
        diagnosis: 'Diagnose',
        dry_run: 'Dry-Run',
        install_module_dry_run: 'Installations-Dry-Run'
    };
    const title = titleMap[action] || 'Dry-Run';
    const summary = action === 'diagnosis'
        ? (healthy ? 'Das Modul wirkt gesund.' : 'Das Modul braucht Aufmerksamkeit.')
        : esc(block.summary || 'Es wurden keine Änderungen ausgeführt.');
    const depItems = Object.entries(deps).map(([key, value]) => {
        const ok = value && value.healthy;
        return `<li>${boolBadge(ok, 'OK', 'offen', true)} ${esc(key)}</li>`;
    }).join('');
    const stepItems = steps.map(step => `<li>${esc(step)}</li>`).join('');
    const blockedHtml = blocked.length
        ? `<div class="mt-2"><strong class="warn">Blocker:</strong><ul class="result-list">${blocked.map(x => `<li>${esc(x)}</li>`).join('')}</ul></div>`
        : `<div class="ok mt-2"><i class="fas fa-check me-1"></i>Keine Blocker erkannt.</div>`;
    const requiredFiles = (block.required_files || []).map(path => `<li><span class="small-code">${esc(path)}</span></li>`).join('');
    const sudoers = (block.required_sudoers || []).map(item => `<li>${esc(item)}</li>`).join('');
    return `
        <div class="result-title"><i class="fas ${icon}"></i>${esc(title)}: ${esc(module.display_name || module.key || 'Modul')}</div>
        <div class="text-secondary small">${summary}</div>
        ${action === 'diagnosis' ? renderDiagnosisSummary(block) : ''}
        ${readinessHtml}
        <div class="result-grid">
            <div class="result-tile"><strong>Dienst</strong>${boolBadge(active, 'aktiv', installed ? 'inaktiv' : 'fehlt', installed)}<div class="text-secondary mt-1">${esc(systemd.unit || module.service_unit || '')}</div></div>
            <div class="result-tile"><strong>Daten</strong>${boolBadge(fresh, 'frisch', alive.exists ? 'veraltet' : 'kein Signal', alive.exists)}<div class="text-secondary mt-1">${alive.age_s !== null && alive.age_s !== undefined ? esc(alive.age_s + 's alt') : esc(alive.path || 'keine Alive-Datei')}</div></div>
            <div class="result-tile"><strong>Konfig</strong>${boolBadge(cfgOk, 'OK', 'unvollständig', true)}<div class="text-secondary mt-1">${config.missing_keys && config.missing_keys.length ? esc(configDisplayList(config, 'missing_labels', 'missing_keys').join(', ')) : 'keine offenen Pflichtfelder'}</div></div>
            <div class="result-tile"><strong>Script</strong>${boolBadge(block.script ? block.script.exists : true, 'vorhanden', 'fehlt', true)}<div class="text-secondary mt-1">${esc((block.script && block.script.path) || module.script || '')}</div></div>
            ${action === 'install_module_dry_run' ? `<div class="result-tile"><strong>Installationsbedarf</strong>${boolBadge(Boolean(block.would_change), 'wäre nötig', 'nicht nötig', true)}<div class="text-secondary mt-1">Dry-Run ohne Änderung</div></div>` : ''}
        </div>
        ${depItems ? `<div><strong>Abhängigkeiten</strong><ul class="result-list">${depItems}</ul></div>` : ''}
        ${stepItems ? `<div class="mt-2"><strong>Geprüfte Schritte</strong><ul class="result-list">${stepItems}</ul></div>` : ''}
        ${installPlanHtml}
        ${requiredFiles ? `<div class="mt-2"><strong>Beteiligte Dateien</strong><ul class="result-list">${requiredFiles}</ul></div>` : ''}
        ${sudoers ? `<div class="mt-2"><strong>Sicherheitsrahmen</strong><ul class="result-list">${sudoers}</ul></div>` : ''}
        ${blockedHtml}
        ${action === 'diagnosis' ? renderLogPreview(logPreview) + renderJournalPreview(journalPreview) : ''}
        ${renderRawDetails(data)}
    `;
}

function renderPermissionsResult(data, action) {
    const checks = data.checks || (data.permissions && data.permissions.checks) || [];
    const issueCount = data.issue_count ?? (data.permissions && data.permissions.issue_count) ?? 0;
    const ok = issueCount === 0;
    const title = action === 'repair_permissions_dry_run' ? 'Reparatur Dry-Run' : 'Nur Rechte prüfen (read-only)';
    const steps = data.planned_steps || [];
    const repairableCount = Number(data.runtime_repairable_issue_count || 0);
    const systemRepairCount = Number(data.system_repair_required_count || 0);
    const repairAvailable = Boolean(data.repair_available);
    const rows = checks.map(item => {
        const isOk = item.ok;
        const repairClass = item.repair_class || '';
        const classBadge = isOk
            ? ''
            : repairClass === 'runtime_repairable'
                ? ' <span class="badge text-bg-info">Reparierbar</span>'
                : repairClass === 'system_repair_required'
                    ? ' <span class="badge text-bg-warning">Systemabgleich</span>'
                    : ' <span class="badge text-bg-secondary">Nur melden</span>';
        return `<li>${boolBadge(isOk, 'OK', 'Prüfen', true)}${classBadge} <span class="small-code">${esc(item.path)}</span>${item.issue ? ` <span class="text-secondary">- ${esc(item.issue)}</span>` : ''}</li>`;
    }).join('');
    const stepItems = steps.map(step => `<li>${esc(step)}</li>`).join('');
    const installPath = data.detected_install_path || '';
    const repairCommand = data.repair_command || '';
    const systemRepairCommand = data.system_repair_command || '';
    const instruction = data.repair_instruction || data.repair_message || '';
    const repairButton = repairableCount > 0 && repairAvailable
        ? `<div class="mt-3"><button class="btn btn-warning rounded-pill" type="button" onclick="runRuntimePermissionsRepair()"><i class="fas fa-screwdriver-wrench me-1"></i> Rechte reparieren</button><div class="text-secondary small mt-2">Releasegleiche Einträge werden sofort repariert. Lokal geänderte Dateien werden vollständig angezeigt und erst nach Deiner exakten Bestätigung berücksichtigt; ihr Inhalt bleibt unverändert.</div></div>`
        : repairableCount > 0
            ? `<div class="warn mt-3">Der enge Rechte-Launcher ist noch nicht sicher installiert. Bis dahin bleibt der Konsolen- oder vollständige Systemweg erforderlich.</div>`
            : '';
    return `
        <div class="result-title"><i class="fas ${ok ? 'fa-circle-check ok' : 'fa-triangle-exclamation warn'}"></i>${esc(title)}</div>
        <div class="text-secondary small">${esc(data.summary || 'Prüfung abgeschlossen.')}</div>
        <div class="result-grid">
            <div class="result-tile"><strong>Ergebnis</strong>${boolBadge(ok, 'alles OK', issueCount + ' Hinweis(e)', true)}</div>
            <div class="result-tile"><strong>Reine Rechtereparatur</strong>${boolBadge(repairAvailable, 'verfügbar', 'nicht verfügbar', true)}<div class="text-secondary mt-1">${esc(repairableCount + ' reparierbare Befund(e)')}</div></div>
            <div class="result-tile"><strong>Vollständiger Systemabgleich</strong>${boolBadge(systemRepairCount === 0, 'nicht nötig', systemRepairCount + ' Befund(e)', true)}<div class="text-secondary mt-1">Bleibt als getrennte Aktion erhalten</div></div>
            <div class="result-tile"><strong>Installationspfad</strong><div class="text-secondary mt-1 small-code">${esc(installPath || 'nicht erkannt')}</div></div>
        </div>
        ${repairButton}
        ${repairCommand ? `<details class="mt-3"><summary><strong>Konsolen-Rückfallweg für reine Rechte</strong></summary><div class="text-secondary small mt-1">${esc(instruction)}</div><pre class="raw-json mt-2">${esc(repairCommand)}</pre></details>` : ''}
        ${systemRepairCommand ? `<details class="mt-2"><summary><strong>Vollständiger Systemabgleich</strong></summary><div class="text-secondary small mt-1">Nur für fehlende oder unsichere Dateien beziehungsweise einen gewünschten Stable-Abgleich; mit Backup und Dienstneustart.</div><pre class="raw-json mt-2">${esc(systemRepairCommand)}</pre></details>` : ''}
        ${stepItems ? `<div><strong>Geplanter Ablauf</strong><ul class="result-list">${stepItems}</ul></div>` : ''}
        ${rows ? `<div class="mt-2"><strong>Geprüfte Pfade</strong><ul class="result-list">${rows}</ul></div>` : ''}
        ${renderRawDetails(data)}
    `;
}

function renderWriteRepairResult(data) {
    const readiness = data.readiness || {};
    const steps = data.steps || [];
    const rollback = data.rollback_plan || [];
    const stepRows = steps.map(item => {
        const label = item.step || 'Schritt';
        const detail = item.message || item.path || item.command || item.output || '';
        return `<li>${boolBadge(Boolean(item.ok), 'OK', 'Prüfen', true)} <strong>${esc(label)}</strong>${detail ? ` <span class="text-secondary">- ${esc(String(detail))}</span>` : ''}</li>`;
    }).join('');
    const rollbackRows = rollback.map(step => `<li>${esc(step)}</li>`).join('');
    const hardBlockers = readiness.hard_blocker_count || 0;
    const writePath = data.privileged_installer_web_enabled ? 'enger Launcher' : 'gesperrt';
    return `
        <div class="result-title"><i class="fas ${data.success ? 'fa-circle-check ok' : 'fa-triangle-exclamation warn'}"></i>Rechte-Reparatur</div>
        <div class="text-secondary small">${esc(data.message || 'Schreibender Wrapper-Job abgeschlossen.')}</div>
        <div class="result-grid">
            <div class="result-tile"><strong>Backup</strong><span class="small-code">${esc(data.backup || 'nicht nötig')}</span></div>
            <div class="result-tile"><strong>Freigabe</strong>${boolBadge(hardBlockers === 0, 'bereit', hardBlockers + ' Blocker', true)}</div>
            <div class="result-tile"><strong>Schreibpfad</strong>${boolBadge(true, writePath, 'direkt', true)}<div class="text-secondary mt-1">kein freier Shell-Befehl</div></div>
        </div>
        ${renderBackupSnapshot(data.backup_snapshot)}
        ${stepRows ? `<div class="mt-2"><strong>Ausgeführte Schritte</strong><ul class="result-list">${stepRows}</ul></div>` : ''}
        ${rollbackRows ? `<div class="mt-2"><strong>Rückweg</strong><ul class="result-list">${rollbackRows}</ul></div>` : ''}
        ${renderRawDetails(data)}
    `;
}

function renderReadinessResult(data) {
    const checks = data.checks || [];
    const hardBlockers = data.hard_blocker_count || 0;
    const ready = Boolean(data.privileged_installer_web_enabled && data.ready_for_manual_enable);
    const rows = checks.map(item => {
        const ok = Boolean(item.ok);
        const label = item.label || item.path || 'Check';
        const detail = item.issue || item.status || item.path || '';
        const badge = boolBadge(ok, ok ? 'OK' : 'Hinweis', ok ? 'Prüfen' : 'Blocker', !item.hard);
        return `<li>${badge} <strong>${esc(label)}</strong>${detail ? ` <span class="text-secondary">- ${esc(detail)}</span>` : ''}</li>`;
    }).join('');
    const actions = (data.allowed_write_actions || []).map(action => `<span class="badge text-bg-secondary me-1 mb-1">${esc(action)}</span>`).join('');
    const releaseSteps = (data.release_steps || []).map(step => `<li>${esc(step)}</li>`).join('');
    return `
        <div class="result-title"><i class="fas ${ready ? 'fa-circle-check ok' : 'fa-triangle-exclamation warn'}"></i>Freigabe-Check</div>
        <div class="text-secondary small">${esc(data.summary || 'Freigabe geprüft.')}</div>
        <div class="result-grid">
            <div class="result-tile"><strong>Schreibmodus</strong>${boolBadge(Boolean(data.write_actions_enabled), 'freigeschaltet', 'gesperrt', true)}</div>
            <div class="result-tile"><strong>Freigabe bereit</strong>${boolBadge(ready, 'vorbereitet', 'nicht bereit', true)}</div>
            <div class="result-tile"><strong>Harte Blocker</strong>${boolBadge(hardBlockers === 0, 'keine', hardBlockers + ' Blocker', true)}</div>
        </div>
        ${rows ? `<div class="mt-2"><strong>Sicherheitsprüfungen</strong><ul class="result-list">${rows}</ul></div>` : ''}
        ${actions ? `<div class="mt-2"><strong>Vorbereitete Schreibaktionen</strong><div class="mt-1">${actions}</div></div>` : ''}
        ${releaseSteps ? `<div class="mt-2"><strong>Nächste Sicherheitsstufe</strong><ul class="result-list">${releaseSteps}</ul></div>` : ''}
        <div class="mt-2 text-secondary small">${esc(data.next_step || '')}</div>
        ${renderRawDetails(data)}
    `;
}

function renderPermissionPlanResult(data) {
    const current = data.current || {};
    const target = data.target || {};
    const readiness = data.readiness || {};
    const filePreview = data.file_preview || {};
    const fileFindings = current.file_findings || {};
    const removeLines = current.direct_systemctl_lines || [];
    const legacyLines = current.legacy_lines || [];
    const directWebLines = fileFindings.direct_web_lines || [];
    const affectedFiles = fileFindings.affected_files || [];
    const missingLines = target.missing_lines || [];
    const allowedLines = target.allowed_lines || [];
    const fileRemoved = filePreview.removed_lines || [];
    const fileMissing = filePreview.missing_lines || [];
    const targetContent = filePreview.target_content || '';
    const steps = data.planned_steps || [];
    const rollback = data.rollback_plan || [];
    const validation = data.validation_commands || [];
    const safety = data.safety_rules || [];
    const hardBlockers = readiness.hard_blocker_count || 0;
    const lineList = (lines, emptyText = 'keine') => lines.length
        ? `<ul class="result-list">${lines.map(line => `<li><span class="small-code">${esc(line)}</span></li>`).join('')}</ul>`
        : `<div class="text-secondary small">${esc(emptyText)}</div>`;
    const findingList = (items, emptyText = 'keine') => items.length
        ? `<ul class="result-list">${items.map(item => `<li><span class="small-code">${esc(item.file || '')}:${esc(item.line_no || '')}</span> ${esc(item.line || '')}</li>`).join('')}</ul>`
        : `<div class="text-secondary small">${esc(emptyText)}</div>`;
    const normalList = (items) => items.length ? `<ul class="result-list">${items.map(item => `<li>${esc(item)}</li>`).join('')}</ul>` : '';
    return `
        <div class="result-title"><i class="fas fa-file-shield text-info me-1"></i>Freigabe-Plan</div>
        <div class="text-secondary small">${esc(data.summary || 'Read-only Plan berechnet.')}</div>
        <div class="result-grid">
            <div class="result-tile"><strong>Modus</strong>${boolBadge(Boolean(data.read_only), 'read-only', 'Schreibpfad', true)}<div class="text-secondary mt-1">${esc(data.sudoers_source || 'unbekannt')}</div></div>
            <div class="result-tile"><strong>Würde ändern</strong>${boolBadge(Boolean(data.would_change), 'ja', 'nein', true)}<div class="text-secondary mt-1">noch keine Ausführung</div></div>
            <div class="result-tile"><strong>Freigabe-Blocker</strong>${boolBadge(hardBlockers === 0, 'keine', hardBlockers + ' Blocker', true)}<div class="text-secondary mt-1">aus aktuellem Check</div></div>
        </div>
        <div class="mt-2"><strong>Ziel-sudoers</strong>${lineList(allowedLines)}</div>
        <div class="mt-2"><strong>Fehlende Zielzeilen</strong>${lineList(missingLines)}</div>
        <div class="mt-2"><strong>Zu entfernende direkte systemctl-Freigaben</strong>${lineList(removeLines)}</div>
        <div class="mt-2"><strong>Alte direkte www-data-Freigaben in sudoers.d</strong>${findingList(directWebLines)}</div>
        <div class="mt-2"><strong>Betroffene sudoers-Dateien</strong>${lineList(affectedFiles)}</div>
        <div class="mt-2"><strong>Zu entfernende Legacy-Freigaben</strong>${lineList(legacyLines)}</div>
        <div class="mt-2"><strong>Ziel-Datei nach Reparatur</strong>
            <div class="text-secondary small">Aus der echten sudoers-Datei entfernt: ${esc(fileRemoved.length)} Zeile(n), fehlend: ${esc(fileMissing.length)} Zeile(n).</div>
            ${targetContent ? `<pre class="raw-json">${esc(targetContent)}</pre>` : '<div class="text-secondary small">kein Zielinhalt berechnet</div>'}
        </div>
        ${steps.length ? `<div class="mt-2"><strong>Späterer Ablauf</strong>${normalList(steps)}</div>` : ''}
        ${validation.length ? `<div class="mt-2"><strong>Validierung</strong>${lineList(validation)}</div>` : ''}
        ${rollback.length ? `<div class="mt-2"><strong>Rückweg</strong>${normalList(rollback)}</div>` : ''}
        ${safety.length ? `<div class="mt-2"><strong>Sicherheitsregeln</strong>${normalList(safety)}</div>` : ''}
        ${renderRawDetails(data)}
    `;
}

function renderBackupPlanResult(data) {
    const items = data.items || [];
    const steps = data.planned_steps || [];
    const rollback = data.rollback_plan || [];
    const safety = data.safety_rules || [];
    const grouped = items.reduce((acc, item) => {
        const key = item.category || 'sonstiges';
        if (!acc[key]) acc[key] = [];
        acc[key].push(item);
        return acc;
    }, {});
    const labels = {
        config: 'Config',
        history: 'History / Ramdisk',
        system: 'System / Wrapper'
    };
    const listItems = (rows) => rows.length
        ? `<ul class="result-list">${rows.map(item => `<li>${boolBadge(Boolean(item.exists), 'vorhanden', 'fehlt', true)} <span class="small-code">${esc(item.path)}</span>${item.exists ? `<div class="text-secondary small">Ziel: <span class="small-code">${esc(item.backup_target || '')}</span></div>` : ''}</li>`).join('')}</ul>`
        : '<div class="text-secondary small">keine Einträge</div>';
    const groupedHtml = Object.keys(grouped).map(key => `
        <div class="mt-2">
            <strong>${esc(labels[key] || key)}</strong>
            ${listItems(grouped[key])}
        </div>
    `).join('');
    const normalList = (rows) => rows.length ? `<ul class="result-list">${rows.map(row => `<li>${esc(row)}</li>`).join('')}</ul>` : '';
    return `
        <div class="result-title"><i class="fas fa-box-archive text-info me-1"></i>Backup-Plan</div>
        <div class="text-secondary small">${esc(data.summary || 'Read-only Backup-Plan berechnet.')}</div>
        <div class="result-grid">
            <div class="result-tile"><strong>Modus</strong>${boolBadge(Boolean(data.read_only), 'read-only', 'Schreibpfad', true)}</div>
            <div class="result-tile"><strong>Würde sichern</strong>${esc(data.would_backup_count ?? 0)} Datei(en)<div class="text-secondary mt-1">${esc(data.missing_count ?? 0)} fehlen aktuell</div></div>
            <div class="result-tile"><strong>Backup-Root</strong><span class="small-code">${esc(data.backup_root || '')}</span></div>
        </div>
        ${groupedHtml}
        ${steps.length ? `<div class="mt-2"><strong>Späterer Ablauf</strong>${normalList(steps)}</div>` : ''}
        ${rollback.length ? `<div class="mt-2"><strong>Rückweg</strong>${normalList(rollback)}</div>` : ''}
        ${safety.length ? `<div class="mt-2"><strong>Sicherheitsregeln</strong>${normalList(safety)}</div>` : ''}
        ${renderRawDetails(data)}
    `;
}

function renderBackupSnapshot(snapshot) {
    if (!snapshot) return '';
    const copied = snapshot.copied || [];
    const skipped = snapshot.skipped || [];
    const retention = snapshot.retention || null;
    const retentionOk = !retention || (
        retention.success !== false
        && !retention.blocked
        && retention.limit_satisfied !== false
    );
    const retentionDetail = retention
        ? (retention.blocker || retention.error || (retentionOk
            ? `maximal ${retention.keep_count ?? 3} Generationen`
            : 'geschützte oder nicht sicher bereinigbare Sicherungen bleiben erhalten'))
        : '';
    const copyRows = copied.slice(0, 8).map(item => `
        <li><i class="fas fa-check-circle ok me-1"></i><span class="small-code">${esc(item.path || '')}</span>
            <div class="text-secondary small">Backup: <span class="small-code">${esc(item.backup || '')}</span></div>
        </li>
    `).join('');
    const skippedRows = skipped.slice(0, 8).map(item => `
        <li><i class="fas fa-triangle-exclamation warn me-1"></i><span class="small-code">${esc(item.path || '')}</span>
            <span class="text-secondary">- ${esc(item.reason || 'übersprungen')}</span>
        </li>
    `).join('');
    const moreCopied = copied.length > 8 ? `<div class="text-secondary small">+ ${esc(copied.length - 8)} weitere gesicherte Datei(en)</div>` : '';
    const moreSkipped = skipped.length > 8 ? `<div class="text-secondary small">+ ${esc(skipped.length - 8)} weitere übersprungene Datei(en)</div>` : '';
    return `
        <div class="mt-2">
            <strong><i class="fas fa-box-archive text-info me-1"></i>Backup-Snapshot</strong>
            <div class="result-grid mt-2">
                <div class="result-tile"><strong>Status</strong>${boolBadge(Boolean(snapshot.success), 'angelegt', 'Fehler', true)}</div>
                <div class="result-tile"><strong>Gesichert</strong>${esc(snapshot.copied_count ?? copied.length)} Datei(en)<div class="text-secondary mt-1">${esc(snapshot.skipped_count ?? skipped.length)} übersprungen</div></div>
                <div class="result-tile"><strong>Pfad</strong><span class="small-code">${esc(snapshot.root || '')}</span></div>
                ${retention ? `<div class="result-tile"><strong>Aufbewahrung</strong>${boolBadge(retentionOk, 'Limit angewendet', 'Grenze offen', true)}<div class="text-secondary mt-1">${esc(retentionDetail)}</div></div>` : ''}
            </div>
            ${retention && !retentionOk ? '<div class="warn small mt-2"><i class="fas fa-triangle-exclamation me-1"></i>Der neue Snapshot bleibt gültig; die Aufbewahrungsgrenze konnte noch nicht vollständig angewendet werden.</div>' : ''}
            ${copyRows ? `<ul class="result-list">${copyRows}</ul>${moreCopied}` : '<div class="text-secondary small">Keine vorhandenen Dateien mussten gesichert werden.</div>'}
            ${skippedRows ? `<div class="mt-2"><strong>Übersprungen</strong><ul class="result-list">${skippedRows}</ul>${moreSkipped}</div>` : ''}
        </div>
    `;
}

function renderJobStatusResult(data) {
    const last = data.last_job || {};
    const job = last.job || {};
    const result = last.result || {};
    const state = last.state || 'kein Job';
    const readOnly = Boolean(last.read_only);
    const running = state === 'running' || Boolean(data.lock_active);
    const ok = state === 'done' || state === 'kein Job';
    const updated = last.updated_at || data.updated_at || '';
    const resultMessage = result.message || result.summary || result.error || '';
    const progressHtml = renderProgressSteps(last.progress_steps || []);
    return `
        <div class="result-title"><i class="fas ${running ? 'fa-spinner fa-spin warn' : (ok ? 'fa-circle-check ok' : 'fa-triangle-exclamation warn')}"></i>Job-Status</div>
        <div class="text-secondary small">Letzter Web-Installer-Job aus der Ramdisk. Diagnose, Direkt-Dry-Run und Statusabfragen sind passiv und überschreiben diesen Stand nicht.</div>
        <div class="result-grid">
            <div class="result-tile"><strong>Status</strong>${boolBadge(ok && !running, jobStateLabel(state), jobStateLabel(state), true)}<div class="text-secondary mt-1">${esc(updated || 'noch kein Zeitstempel')}</div></div>
            <div class="result-tile"><strong>Job</strong>${esc(actionLabel(job.action))}<div class="text-secondary mt-1">${job.module ? esc(job.module) : 'kein Modul'}</div></div>
            <div class="result-tile"><strong>Ausführung</strong>${boolBadge(readOnly, 'read-only', 'Schreibpfad', true)}<div class="text-secondary mt-1">${data.lock_active ? 'Lock aktiv' : 'kein aktiver Lock'}</div></div>
            <div class="result-tile"><strong>Ergebnis</strong>${result.success === false ? '<span class="bad">Fehler</span>' : '<span class="ok">OK</span>'}<div class="text-secondary mt-1">${esc(resultMessage || 'noch kein Ergebnis')}</div></div>
        </div>
        <div class="mt-2"><strong>Statusdateien</strong>
            <ul class="result-list">
                <li><span class="small-code">${esc(data.status_file || '')}</span></li>
                <li><span class="small-code">${esc(data.job_file || '')}</span></li>
                <li><span class="small-code">${esc(data.lock_file || '')}</span></li>
            </ul>
        </div>
        ${progressHtml}
        ${renderRawDetails(data)}
    `;
}

function renderInstallModuleWriteResult(data) {
    const module = data.module || {};
    const steps = data.steps || [];
    const rows = steps.map(step => {
        const ok = Boolean(step.ok);
        const detail = step.target || step.cmd || step.stderr || step.stdout || step.backup || '';
        return `<li>${ok ? '<i class="fas fa-check-circle ok me-1"></i>' : '<i class="fas fa-circle-xmark bad me-1"></i>'}<strong>${esc(step.step || 'Schritt')}</strong>${detail ? ` <span class="text-secondary">- ${esc(String(detail).slice(0, 220))}</span>` : ''}</li>`;
    }).join('');
    return `
        <div class="result-title"><i class="fas ${data.success ? 'fa-circle-check ok' : 'fa-triangle-exclamation warn'}"></i>Modulinstallation</div>
        <div>${esc(data.message || 'Modulinstallation abgeschlossen.')}</div>
        <div class="result-grid mt-2">
            <div class="result-tile"><strong>Modul</strong><div class="text-secondary mt-1">${esc(module.display_name || module.key || '')}</div></div>
            <div class="result-tile"><strong>Dienst</strong><div class="text-secondary mt-1">${esc(module.service_unit || '')}</div></div>
            <div class="result-tile"><strong>Ergebnis</strong>${boolBadge(Boolean(data.success), 'OK', 'prüfen', true)}</div>
        </div>
        ${renderBackupSnapshot(data.backup_snapshot)}
        ${rows ? `<div class="mt-2"><strong>Ausgeführte Schritte</strong><ul class="result-list">${rows}</ul></div>` : ''}
        ${renderRawDetails(data)}
    `;
}

function renderRemoveModuleWriteResult(data) {
    const module = data.module || {};
    const steps = data.steps || [];
    const rollbackRows = (data.rollback_plan || []).map(item => `<li>${esc(item)}</li>`).join('');
    const rows = steps.map(step => {
        const ok = Boolean(step.ok);
        const detail = step.target || step.path || step.stderr || step.stdout || step.message || '';
        return `<li>${ok ? '<i class="fas fa-check-circle ok me-1"></i>' : '<i class="fas fa-circle-xmark bad me-1"></i>'}<strong>${esc(step.step || 'Schritt')}</strong>${detail ? ` <span class="text-secondary">- ${esc(String(detail).slice(0, 220))}</span>` : ''}</li>`;
    }).join('');
    return `
        <div class="result-title"><i class="fas ${data.success ? 'fa-circle-check ok' : 'fa-triangle-exclamation warn'}"></i>Modul-Rückbau</div>
        <div>${esc(data.message || 'Rückbau abgeschlossen.')}</div>
        <div class="result-grid mt-2">
            <div class="result-tile"><strong>Modul</strong><div class="text-secondary mt-1">${esc(module.display_name || module.key || '')}</div></div>
            <div class="result-tile"><strong>Dienst</strong><div class="text-secondary mt-1">${esc(module.service_unit || '')}</div></div>
            <div class="result-tile"><strong>Ergebnis</strong>${boolBadge(Boolean(data.success), 'OK', 'prüfen', true)}</div>
        </div>
        ${renderBackupSnapshot(data.backup_snapshot)}
        ${rows ? `<div class="mt-2"><strong>Ausgeführte Schritte</strong><ul class="result-list">${rows}</ul></div>` : ''}
        ${rollbackRows ? `<div class="mt-2"><strong>Rückweg</strong><ul class="result-list">${rollbackRows}</ul></div>` : ''}
        ${renderRawDetails(data)}
    `;
}

function renderServiceControlResult(data, service, action) {
    if (!data || data.success === false) {
        if (data && data.error_code === 'unit_missing') {
            return `
                <div class="result-title"><i class="fas fa-circle-info warn"></i>Dienst fehlt noch</div>
                <div>${esc(data.message || 'Die Service-Datei ist noch nicht installiert.')}</div>
                <div class="mt-2 text-secondary">${esc(data.hint || 'Bitte zuerst den Installations-Check ausführen.')}</div>
                <details class="mt-2"><summary class="text-secondary small">Technische Details</summary><div class="raw-json">${esc(prettyJson(data))}</div></details>
            `;
        }
        return `<div class="result-title"><i class="fas fa-circle-xmark bad"></i>Dienstaktion fehlgeschlagen</div><div>${esc(data && (data.error || data.message) || 'Unbekannter Fehler')}</div>${renderRawDetails(data || {})}`;
    }
    const output = data.output ? `<pre class="raw-json">${esc(data.output)}</pre>` : '';
    const noop = data.noop ? `<div class="mt-2 text-secondary">${esc(data.message || 'Keine Änderung nötig.')}</div>` : '';
    return `
        <div class="result-title"><i class="fas fa-circle-check ok"></i>${data.noop ? 'Keine Dienstaktion nötig' : 'Dienstaktion ausgeführt'}</div>
        <div class="result-grid">
            <div class="result-tile"><strong>Dienst</strong><div class="text-secondary mt-1">${esc(service)}</div></div>
            <div class="result-tile"><strong>Aktion</strong><div class="text-secondary mt-1">${esc(action)}</div></div>
            <div class="result-tile"><strong>Status</strong><div class="text-secondary mt-1">${esc(data.status || 'unbekannt')}</div></div>
            <div class="result-tile"><strong>Autostart</strong>${boolBadge(Boolean(data.enabled), 'aktiv', 'aus', true)}</div>
        </div>
        ${noop}
        ${output}
        ${renderRawDetails(data)}
    `;
}

function renderActionResult(data, action) {
    if (!data || data.success === false) {
        return `<div class="result-title"><i class="fas fa-circle-xmark bad"></i>Aktion fehlgeschlagen</div><div>${esc(data && (data.error || data.message) || 'Unbekannter Fehler')}</div>${renderRawDetails(data || {})}`;
    }
    if (action === 'install_module_dry_run' && data.install_dry_run && Object.keys(data.install_dry_run).length > 1) return renderReadinessOverview(data);
    if (action === 'diagnosis' || action === 'dry_run' || action === 'install_module_dry_run') return renderModuleResult(data, action);
    if (action === 'permissions_check' || action === 'repair_permissions_dry_run') return renderPermissionsResult(data, action);
    if (action === 'repair_permissions') return renderWriteRepairResult(data);
    if (action === 'install_module') return renderInstallModuleWriteResult(data);
    if (action === 'remove_module') return renderRemoveModuleWriteResult(data);
    if (action === 'write_readiness') return renderReadinessResult(data);
    if (action === 'write_permission_plan') return renderPermissionPlanResult(data);
    if (action === 'backup_plan') return renderBackupPlanResult(data);
    if (action === 'job_status') return renderJobStatusResult(data);
    return `<div class="result-title"><i class="fas fa-circle-info ok"></i>Rückmeldung</div>${renderRawDetails(data)}`;
}

async function showModuleDiagnosis(moduleKey) {
    const module = installCenterModules[moduleKey] || {};
    const name = module.display_name || moduleKey;
    showJobModal(
        `<i class="fas fa-stethoscope text-info me-2"></i>Diagnose: ${esc(name)}`,
        `${esc(module.service_unit || module.service || moduleKey)} · liest nur Status, Config, Logs und Journal`,
        '<div class="job-progress-box"><i class="fas fa-spinner fa-spin warn me-1"></i>Diagnose wird gelesen...</div>',
        true
    );
    try {
        const data = await loadJson(`install_center.php?action=diagnosis&module=${encodeURIComponent(moduleKey)}`);
        const supportButton = `
            <div class="mt-3 d-flex flex-wrap gap-2">
                <button class="btn btn-sm btn-outline-warning" onclick="showDiagnosticBundleModal()"><i class="fas fa-file-zipper me-1"></i>Diagnosepaket erstellen</button>
                ${module.config_keys && module.config_keys.length ? `<button class="btn btn-sm btn-outline-info" onclick="showConfigModal('${esc(moduleKey)}')"><i class="fas fa-sliders me-1"></i>Modul-Config öffnen</button>` : ''}
            </div>
        `;
        const html = renderActionResult(data, 'diagnosis') + supportButton;
        document.getElementById('jobModalBody').innerHTML = html;
        document.getElementById('actionLog').innerHTML = html;
    } catch (err) {
        const html = `<div class="bad">Diagnose konnte nicht gelesen werden: ${esc(err.message || err)}</div>`;
        document.getElementById('jobModalBody').innerHTML = html;
        document.getElementById('actionLog').innerHTML = html;
    }
}

async function runModuleAction(moduleKey, action) {
    const log = document.getElementById('actionLog');
    log.textContent = `Lese ${action} für ${moduleKey}...`;
    try {
        const data = await loadJson(`install_center.php?action=${encodeURIComponent(action)}&module=${encodeURIComponent(moduleKey)}`);
        log.innerHTML = renderActionResult(data, action);
    } catch (err) {
        log.textContent = `Fehler: ${err.message}`;
    }
}

const INSTALL_CENTER_UPDATE_POLL_TIMEOUT_MS = 10000;
const INSTALL_CENTER_UPDATE_START_TIMEOUT_MS = 30000;

function normalizePermissionRepairRunId(value) {
    const normalized = (typeof value === 'string') ? value.trim().toLowerCase() : '';
    return /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(normalized)
        ? normalized
        : null;
}

async function loadUpdateJsonWithTimeout(url, timeoutMs = INSTALL_CENTER_UPDATE_POLL_TIMEOUT_MS) {
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    let timeoutId = null;
    const timeoutPromise = new Promise((_resolve, reject) => {
        timeoutId = window.setTimeout(() => {
            if (controller) controller.abort();
            const error = new Error('Statusabfrage überschritt das Zeitlimit');
            error.name = 'AbortError';
            reject(error);
        }, timeoutMs);
    });
    const requestPromise = fetch(url, {
        cache: 'no-store',
        credentials: 'same-origin',
        signal: controller ? controller.signal : undefined
    }).then(async response => {
        if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
        return await response.json();
    });
    return await Promise.race([requestPromise, timeoutPromise])
        .finally(() => {
            if (timeoutId !== null) window.clearTimeout(timeoutId);
        });
}

async function postPermissionRepairWithTimeout(body, timeoutMs = INSTALL_CENTER_UPDATE_START_TIMEOUT_MS) {
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    let timeoutId = null;
    const timeoutPromise = new Promise((_resolve, reject) => {
        timeoutId = window.setTimeout(() => {
            if (controller) controller.abort();
            const error = new Error('Startantwort überschritt das Zeitlimit');
            error.name = 'AbortError';
            reject(error);
        }, timeoutMs);
    });
    const requestPromise = fetch('index.php?action=fix_permissions', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
            'X-Requested-With': 'XMLHttpRequest',
            'X-CSRF-Token': installCenterCsrfToken
        },
        signal: controller ? controller.signal : undefined,
        body
    });
    return await Promise.race([requestPromise, timeoutPromise])
        .finally(() => {
            if (timeoutId !== null) window.clearTimeout(timeoutId);
        });
}

async function readPermissionRepairDriftPreflight() {
    const body = new FormData();
    body.append('csrf_token', installCenterCsrfToken);
    const response = await fetch(`index.php?action=check_self_update_drift&t=${Date.now()}`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
            'X-Requested-With': 'XMLHttpRequest',
            'X-CSRF-Token': installCenterCsrfToken
        },
        cache: 'no-store',
        body
    });
    const text = await response.text();
    let data = null;
    try { data = JSON.parse(text); } catch (_error) {}
    if (!response.ok || !data || data.success !== true) {
        throw new Error((data && data.message) || text || `HTTP ${response.status}`);
    }
    return data;
}

async function readPermissionRepairBaseline() {
    try {
        const data = await loadUpdateJsonWithTimeout(`index.php?action=poll_self_update&t=${Date.now()}`);
        return {
            available: true,
            runId: normalizePermissionRepairRunId(data && data.run_id),
            running: Boolean(data && data.running === true),
        };
    } catch (_error) {
        return {available: false, runId: null, running: false};
    }
}

function createPermissionRepairRunBinding(baseline, expectedRunId = null) {
    const state = (baseline && typeof baseline === 'object') ? baseline : {};
    const baselineRunId = normalizePermissionRepairRunId(state.runId);
    const expected = normalizePermissionRepairRunId(expectedRunId);
    return {
        baselineAvailable: state.available === true,
        baselineRunId,
        baselineWasRunning: state.running === true,
        expectedRunId: expected,
        boundRunId: null,
    };
}

function bindPermissionRepairPoll(data, binding) {
    const state = (binding && typeof binding === 'object') ? binding : {};
    const currentRunId = normalizePermissionRepairRunId(data && data.run_id);
    if (state.boundRunId) {
        return {
            bound: currentRunId === state.boundRunId,
            replaced: Boolean(currentRunId && currentRunId !== state.boundRunId),
        };
    }
    if (state.expectedRunId) {
        if (currentRunId === state.expectedRunId) {
            state.boundRunId = currentRunId;
        }
    } else if (state.baselineWasRunning && currentRunId === state.baselineRunId
        && data && data.running === true) {
        state.boundRunId = currentRunId;
    } else if (state.baselineRunId && currentRunId && currentRunId !== state.baselineRunId) {
        state.boundRunId = currentRunId;
    } else if (state.baselineAvailable && !state.baselineRunId && currentRunId
        && data && data.running === true) {
        // Beim ersten Lauf nach Einführung der Laufkennung existiert noch keine
        // Baseline-ID. Die danach atomar veröffentlichte ID gehört eindeutig zum
        // neuen Systemjob. Ohne explizite Startantwort wird er erst gebunden,
        // wenn derselbe Folgepoll den Lauf auch aktiv beobachtet.
        state.boundRunId = currentRunId;
    } else if (!state.baselineAvailable && currentRunId && data && data.running === true) {
        state.boundRunId = currentRunId;
    }
    return {
        bound: Boolean(state.boundRunId && currentRunId === state.boundRunId),
        replaced: false,
    };
}

function pollPermissionRepairUpdate(runBinding = null) {
    const log = document.getElementById('actionLog');
    const modalBody = document.getElementById('jobModalBody');
    let ticks = 0;
    let transientErrors = 0;
    let lastOutput = '';
    let pollInFlight = false;
    const maxTransientErrors = 120;
    const binding = runBinding || createPermissionRepairRunBinding({available: false});
    const pollStartedAt = Date.now();
    const maxTransientDurationMs = 2 * 60 * 1000;
    const maxPollDurationMs = 30 * 60 * 1000;
    const timer = window.setInterval(async () => {
        if (pollInFlight) return;
        pollInFlight = true;
        ticks += 1;
        try {
            const data = await loadUpdateJsonWithTimeout(`index.php?action=poll_self_update&t=${Date.now()}`);
            transientErrors = 0;
            const runState = bindPermissionRepairPoll(data, binding);
            if (runState.replaced) {
                window.clearInterval(timer);
                const html = `<div class="warn">Der Status gehört inzwischen zu einem anderen Systemjob. Der Abschluss des gestarteten Reparaturlaufs wird deshalb nicht aus fremden Daten abgeleitet. Lade die Seite neu und prüfe das Update-Protokoll.</div>`;
                log.innerHTML = html;
                if (modalBody) modalBody.innerHTML = html;
                return;
            }
            if (!runState.bound) {
                const html = `
                    <div class="result-title"><i class="fas fa-spinner fa-spin warn"></i>Warte auf eindeutige Laufkennung</div>
                    <div class="text-secondary small mt-2">Ein alter Abschlussstatus wird nicht als Ergebnis dieses Reparaturauftrags übernommen.</div>`;
                log.innerHTML = html;
                if (modalBody) modalBody.innerHTML = html;
                if (ticks >= 1800 || (Date.now() - pollStartedAt) >= maxPollDurationMs) {
                    window.clearInterval(timer);
                    const timeoutHtml = `<div class="warn">Kein eindeutig zugeordneter Abschlussstatus. Der Systemjob wurde dadurch nicht beendet. Lade die Seite neu und prüfe das Update-Protokoll.</div>`;
                    log.innerHTML = timeoutHtml;
                    if (modalBody) modalBody.innerHTML = timeoutHtml;
                }
                return;
            }
            const output = typeof data.log === 'string' ? data.log : '';
            lastOutput = output || lastOutput;
            const state = data.completion || (data.running ? 'running' : 'unknown');
            const finished = state === 'success' || state === 'failed';
            const ok = state === 'success';
            const html = `
                <div class="result-title"><i class="fas ${finished ? (ok ? 'fa-circle-check ok' : 'fa-circle-xmark bad') : 'fa-spinner fa-spin warn'}"></i>${finished ? (ok ? 'Reparatur/Systemabgleich abgeschlossen' : 'Reparatur/Systemabgleich fehlgeschlagen') : 'Reparatur/Systemabgleich läuft'}</div>
                <pre class="raw-json mt-2">${esc(output || 'Warte auf Protokoll...')}</pre>`;
            log.innerHTML = html;
            if (modalBody) modalBody.innerHTML = html;
            if (finished || ticks >= 1800 || (Date.now() - pollStartedAt) >= maxPollDurationMs) {
                window.clearInterval(timer);
                await loadInstallCenter();
            }
        } catch (err) {
            transientErrors += 1;
            if (transientErrors <= maxTransientErrors
                && (Date.now() - pollStartedAt) < maxTransientDurationMs) {
                const html = `
                    <div class="result-title"><i class="fas fa-spinner fa-spin warn"></i>Weboberfläche wird kontrolliert neu gestartet</div>
                    <div class="text-secondary small mt-2">Der root-eigene Systemjob läuft unabhängig weiter. Die Verbindung wird automatisch erneut geprüft.</div>
                    ${lastOutput ? `<pre class="raw-json mt-2">${esc(lastOutput)}</pre>` : ''}`;
                log.innerHTML = html;
                if (modalBody) modalBody.innerHTML = html;
            } else {
                window.clearInterval(timer);
                const html = `<div class="warn">Die Weboberfläche konnte nach zwei Minuten noch nicht wieder erreicht werden. Der Systemjob wurde dadurch nicht beendet. Lade die Seite neu, um den aktuellen Abschlussstatus zu lesen.</div>`;
                log.innerHTML = html;
                if (modalBody) modalBody.innerHTML = html;
            }
        } finally {
            pollInFlight = false;
        }
    }, 1000);
}

async function callRuntimePermissionsLauncher(action, confirmationToken = '') {
    const body = new FormData();
    body.append('csrf_token', installCenterCsrfToken);
    const normalizedToken = String(confirmationToken || '').trim().toLowerCase();
    if (normalizedToken) {
        body.append('confirm_content_drift', '1');
        body.append('confirmation_token', normalizedToken);
    }
    const response = await fetch(`install_center.php?action=${encodeURIComponent(action)}`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {'X-Requested-With': 'XMLHttpRequest'},
        cache: 'no-store',
        body
    });
    const text = await response.text();
    let data = null;
    try { data = JSON.parse(text); } catch (_error) {}
    if (!response.ok || !data) {
        throw new Error((data && (data.message || data.error)) || text || `HTTP ${response.status}`);
    }
    return data;
}

async function runRuntimePermissionsRepair() {
    const log = document.getElementById('actionLog');
    showJobModal(
        '<i class="fas fa-screwdriver-wrench text-warning me-2"></i>Rechte reparieren',
        'Nur Metadaten bekannter Pfade – kein Backup, kein Update, kein Dienstneustart',
        '<div class="job-progress-box"><i class="fas fa-spinner fa-spin warn me-1"></i>Releasegleiche Rechte werden repariert; lokale Inhaltsabweichungen werden exakt gebunden...</div>',
        true
    );
    const modalBody = document.getElementById('jobModalBody');
    try {
        let result = await callRuntimePermissionsLauncher('repair_runtime_permissions');
        if (!result.success) {
            throw new Error(result.message || 'Die Rechtereparatur wurde abgebrochen.');
        }
        const driftCount = Number(result.content_drift_count || 0);
        const driftFiles = Array.isArray(result.content_drift)
            ? result.content_drift.map(path => String(path || ''))
            : [];
        if (driftCount !== driftFiles.length
            || driftFiles.some(path => !path)
            || new Set(driftFiles).size !== driftFiles.length) {
            throw new Error('Die Rechtereparatur lieferte keine vollständige eindeutige Dateiliste.');
        }
        if (result.confirmation_required === true || driftCount > 0) {
            const token = String(result.confirmation_token || '').trim().toLowerCase();
            if (result.confirmation_required !== true
                || driftCount < 1
                || !/^[0-9a-f]{64}$/.test(token)) {
                throw new Error('Die lokale Dateilistenbindung ist unvollständig. Bitte starte die Rechtereparatur erneut.');
            }
            const alreadyChanged = Number(result.changed || 0);
            const question = `${driftCount} bekannte Datei(en) wurden lokal geändert und stimmen nicht mit dem veröffentlichten Release überein.\n\nReleasegleiche Einträge wurden bereits repariert (${alreadyChanged} Metadatenänderung(en)). Die folgenden lokalen Inhalte blieben unangetastet:\n\n${driftFiles.join('\n')}\n\nSoll die Rechtereparatur ausschließlich Besitzer, Gruppe und Modus genau dieser vollständig aufgelisteten Dateien setzen? Ihre Inhalte bleiben unverändert.`;
            if (!confirm(question)) {
                const html = `
                    <div class="warn">Lokale Inhaltsabweichungen wurden nicht bestätigt und blieben vollständig unverändert.</div>
                    <div class="text-secondary small mt-2">Releasegleiche bekannte Einträge wurden bereits repariert: ${esc(alreadyChanged)} Metadatenänderung(en). Für einen späteren Versuch bitte die Rechtereparatur neu starten.</div>
                    <div class="mt-2"><strong>Unveränderte lokale Dateien</strong><ul class="result-list">${driftFiles.map(path => `<li><span class="small-code">${esc(path)}</span></li>`).join('')}</ul></div>`;
                log.innerHTML = html;
                if (modalBody) modalBody.innerHTML = html;
                return;
            }
            const progress = '<div class="job-progress-box"><i class="fas fa-spinner fa-spin warn me-1"></i>Die exakt bestätigten Driftdateien werden erneut gebunden und nur ihre Metadaten repariert...</div>';
            log.innerHTML = progress;
            if (modalBody) modalBody.innerHTML = progress;
            result = await callRuntimePermissionsLauncher(
                'repair_runtime_permissions',
                token
            );
            if (!result.success) {
                throw new Error(result.message || 'Die bestätigte Rechtereparatur wurde sicher abgebrochen.');
            }
        }
        const postcheck = await loadJson(`install_center.php?action=permissions_check&t=${Date.now()}`);
        const remaining = Number(postcheck.issue_count || 0);
        const totalChanged = Number(result.changed || 0) + Number(result.initial_changed || 0);
        const resultHtml = `
            <div class="result-title"><i class="fas fa-circle-check ok"></i>Rechtereparatur abgeschlossen</div>
            <div class="text-secondary small">${esc(result.message || 'Metadaten wurden geprüft und repariert.')}</div>
            <div class="result-grid mt-2">
                <div class="result-tile"><strong>Geprüft</strong><span>${esc(result.checked || 0)}</span></div>
                <div class="result-tile"><strong>Geändert</strong><span>${esc(totalChanged)}</span></div>
                <div class="result-tile"><strong>Lokale Inhalte</strong><span>${esc(result.content_drift_count || 0)} abweichend, unverändert</span></div>
                <div class="result-tile"><strong>Nachprüfung</strong>${boolBadge(remaining === 0, 'alles OK', remaining + ' Hinweis(e)', true)}</div>
            </div>
            ${renderPermissionsResult(postcheck, 'permissions_check')}`;
        log.innerHTML = resultHtml;
        if (modalBody) modalBody.innerHTML = resultHtml;
        await loadInstallCenter();
    } catch (err) {
        const html = `<div class="bad">Rechtereparatur sicher abgebrochen: ${esc(err.message || err)}</div>`;
        log.innerHTML = html;
        if (modalBody) modalBody.innerHTML = html;
    }
}

async function runPermissionRepairUpdate() {
    if (!confirm('Vollständige Systemreparatur starten?\n\nDies ist kein reiner Rechtecheck: Der Systemjob erstellt ein verifiziertes Backup, gleicht alle Produktdateien mit dem veröffentlichten Stable-Stand ab, setzt die Rechte neu und startet die Dienste neu. Dabei kann dieselbe Version erneut installiert werden.')) return;
    const log = document.getElementById('actionLog');
    showJobModal(
        '<i class="fas fa-tools text-warning me-2"></i>System reparieren',
        'Backup + vollständiger Stable-Abgleich + Rechteprojektion + Dienstneustart',
        '<div class="job-progress-box"><i class="fas fa-spinner fa-spin warn me-1"></i>Lokale Inhalte werden vor dem Start rein lesend geprüft...</div>',
        true
    );
    const body = new FormData();
    body.append('csrf_token', installCenterCsrfToken);
    let baseline = {available: false, runId: null, running: false};
    try {
        const drift = await readPermissionRepairDriftPreflight();
        const driftItems = Array.isArray(drift.content_drift) ? drift.content_drift : [];
        const driftCount = Number(drift.content_drift_count || 0);
        if (driftCount !== driftItems.length) {
            throw new Error('Die Inhaltsprüfung lieferte keine vollständige Dateiliste.');
        }
        if (drift.requires_confirmation === true || driftCount > 0) {
            const paths = driftItems.map(item => {
                const path = String(item && item.path || '');
                const status = String(item && item.status || '');
                if (!path) return '';
                return status === 'unknown_retired_file' || status === 'local_retired_content_changed'
                    ? `${path} (freigegebener Altpfad würde gelöscht)`
                    : path;
            }).filter(Boolean);
            const token = String(drift.confirmation_token || '').trim().toLowerCase();
            if (!/^[0-9a-f]{64}$/.test(token)) {
                throw new Error('Die Inhaltsprüfung lieferte keine gültige Dateilistenbindung.');
            }
            const question = driftCount > 0
                ? `${driftCount} lokal geänderte oder kollidierende Produktdatei(en) würden durch den veröffentlichten Stable-Stand ersetzt oder als freigegebener Altpfad gelöscht:\n\n${paths.join('\n')}\n\nUnbekannte Dateien außerhalb dieses Zielumfangs bleiben unberührt. Soll die Systemreparatur diese exakt genannten Eingriffe trotzdem ausführen?`
                : 'Die veröffentlichte Altversion dieser Installation konnte nicht sicher als Inhaltsbaseline gebunden werden. Lokale Änderungen in bekannten Produktpfaden lassen sich deshalb nicht einzeln abgrenzen.\n\nDas verifizierte Vollbackup bleibt bestehen und unbekannte Dateien außerhalb der Zielprojektion bleiben unberührt. Soll die Systemreparatur den veröffentlichten Stable-Stand trotzdem auf die bekannten Produktpfade projizieren?';
            if (!confirm(question)) {
                const html = '<div class="text-secondary">Systemreparatur nach dem Nur-Lese-Preflight abgebrochen. Es wurde nichts geändert.</div>';
                log.innerHTML = html;
                const modalBody = document.getElementById('jobModalBody');
                if (modalBody) modalBody.innerHTML = html;
                return;
            }
            body.append('confirm_local_drift', '1');
            body.append('confirmation_token', token);
        } else {
            body.append('confirm_local_drift', '0');
            body.append('confirmation_token', '');
        }
    } catch (err) {
        const html = `<div class="bad">Systemreparatur wurde vor Backup und Dienständerung sicher abgebrochen: ${esc(err.message || err)}</div>`;
        log.innerHTML = html;
        const modalBody = document.getElementById('jobModalBody');
        if (modalBody) modalBody.innerHTML = html;
        return;
    }
    baseline = await readPermissionRepairBaseline();
    try {
        const response = await postPermissionRepairWithTimeout(body);
        const text = await response.text();
        let data = null;
        try { data = JSON.parse(text); } catch (_error) {}
        if (data && !data.success) {
            const html = `<div class="bad">Systemreparatur wurde nicht gestartet: ${esc(data.message || text || `HTTP ${response.status}`)}</div>`;
            log.innerHTML = html;
            const modalBody = document.getElementById('jobModalBody');
            if (modalBody) modalBody.innerHTML = html;
            return;
        }
        if (!response.ok || !data) {
            const html = `<div class="result-title"><i class="fas fa-spinner fa-spin warn"></i>Startantwort nicht lesbar; Status wird geprüft</div><div class="text-secondary small mt-2">Der Systemjob kann die Weboberfläche bereits für den Dateiaustausch neu starten. Sein kanonischer Abschlussstatus wird unabhängig von dieser Antwort weiter abgefragt.</div>`;
            log.innerHTML = html;
            const modalBody = document.getElementById('jobModalBody');
            if (modalBody) modalBody.innerHTML = html;
            pollPermissionRepairUpdate(createPermissionRepairRunBinding(baseline));
            return;
        }
        const html = `<div class="result-title"><i class="fas fa-spinner fa-spin warn"></i>Systemjob gestartet</div><div class="text-secondary small mt-2">${esc(data.message || 'Backup, Rechteprojektion und Systemabgleich laufen im Hintergrund.')}</div>`;
        log.innerHTML = html;
        const modalBody = document.getElementById('jobModalBody');
        if (modalBody) modalBody.innerHTML = html;
        pollPermissionRepairUpdate(createPermissionRepairRunBinding(baseline, data.run_id));
    } catch (err) {
        const html = `<div class="result-title"><i class="fas fa-spinner fa-spin warn"></i>Startantwort nicht lesbar; Status wird geprüft</div><div class="text-secondary small mt-2">Es liegt noch kein bestätigter Fehler des Systemjobs vor. Der kanonische Abschlussstatus wird weiter abgefragt.</div>`;
        log.innerHTML = html;
        const modalBody = document.getElementById('jobModalBody');
        if (modalBody) modalBody.innerHTML = html;
        pollPermissionRepairUpdate(createPermissionRepairRunBinding(baseline));
    }
}

async function runGlobalAction(action) {
    const log = document.getElementById('actionLog');
    const label = (typeof actionLabel === 'function' ? actionLabel(action) : null) || action;
    log.textContent = `Lese ${label}...`;
    if (typeof showJobModal === 'function') {
        showJobModal(
            `<i class="fas fa-traffic-light text-info me-2"></i>Prüfung läuft`,
            `${esc(label)} · Read-Only Analyse`,
            '<div class="job-progress-box"><i class="fas fa-spinner fa-spin warn me-1"></i>Analysiere Module und Systemstand...</div>',
            true
        );
    }
    try {
        const data = await loadJson(`install_center.php?action=${encodeURIComponent(action)}`);
        const html = renderActionResult(data, action);
        log.innerHTML = html;
        const modalBody = document.getElementById('jobModalBody');
        if (modalBody) modalBody.innerHTML = html;
    } catch (err) {
        const errHtml = `<div class="bad">Prüfung fehlgeschlagen: ${esc(err.message || err)}</div>`;
        log.innerHTML = errHtml;
        const modalBody = document.getElementById('jobModalBody');
        if (modalBody) modalBody.innerHTML = errHtml;
    }
}

async function runModuleJob(moduleKey, action, viaWrapper = false) {
    const log = document.getElementById('actionLog');
    log.innerHTML = '<div class="text-secondary">Starte sicheren Web-Installer-Job...</div>';
    if (viaWrapper) log.innerHTML = '<div class="text-secondary">Starte sicheren Web-Installer-Job via installer_wrapper.sh...</div>';
    showJobModal(
        '<i class="fas fa-clipboard-check text-info me-2"></i>Job-Test läuft',
        `${esc(actionLabel(action))} / ${esc(moduleKey)}`,
        '<div class="job-progress-box"><i class="fas fa-spinner fa-spin warn me-1"></i>Job wird in die Ramdisk geschrieben...</div>'
    );
    if (jobModalRefreshTimer) window.clearInterval(jobModalRefreshTimer);
    jobModalRefreshTimer = window.setInterval(updateJobModalFromStatus, 2500);
    const form = new FormData();
    form.append('job_action', action);
    form.append('module', moduleKey);
    form.append('csrf_token', installCenterCsrfToken);
    try {
        const endpoint = viaWrapper ? 'run_wrapper_job' : 'run_job';
        const res = await fetch(`install_center.php?action=${endpoint}`, {method: 'POST', body: form});
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (!data || data.success !== true) throw new Error((data && (data.error || data.message)) || 'Job-Start wurde nicht bestätigt.');
        log.innerHTML = renderActionResult(data, action);
        await refreshInstallerStatusOnly();
        await updateJobModalFromStatus();
    } catch (err) {
        log.innerHTML = `<div class="bad">Job fehlgeschlagen: ${esc(err.message || err)}</div>`;
        document.getElementById('jobModalBody').innerHTML = `<div class="bad">Job fehlgeschlagen: ${esc(err.message || err)}</div>`;
    } finally {
        if (jobModalRefreshTimer) {
            window.clearInterval(jobModalRefreshTimer);
            jobModalRefreshTimer = null;
        }
    }
}

async function runGlobalJob(action, viaWrapper = false) {
    const log = document.getElementById('actionLog');
    log.innerHTML = '<div class="text-secondary">Starte sicheren Web-Installer-Job...</div>';
    if (viaWrapper) log.innerHTML = '<div class="text-secondary">Starte sicheren Web-Installer-Job via installer_wrapper.sh...</div>';
    showJobModal(
        '<i class="fas fa-clipboard-check text-info me-2"></i>Globaler Job-Test läuft',
        `${esc(actionLabel(action))}`,
        '<div class="job-progress-box"><i class="fas fa-spinner fa-spin warn me-1"></i>Job wird in die Ramdisk geschrieben...</div>'
    );
    if (jobModalRefreshTimer) window.clearInterval(jobModalRefreshTimer);
    jobModalRefreshTimer = window.setInterval(updateJobModalFromStatus, 2500);
    const form = new FormData();
    form.append('job_action', action);
    form.append('csrf_token', installCenterCsrfToken);
    try {
        const endpoint = viaWrapper ? 'run_wrapper_job' : 'run_job';
        const res = await fetch(`install_center.php?action=${endpoint}`, {method: 'POST', body: form});
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (!data || data.success !== true) throw new Error((data && (data.error || data.message)) || 'Job-Start wurde nicht bestätigt.');
        log.innerHTML = renderActionResult(data, action);
        await refreshInstallerStatusOnly();
        await updateJobModalFromStatus();
    } catch (err) {
        log.innerHTML = `<div class="bad">Job fehlgeschlagen: ${esc(err.message || err)}</div>`;
        document.getElementById('jobModalBody').innerHTML = `<div class="bad">Job fehlgeschlagen: ${esc(err.message || err)}</div>`;
    } finally {
        if (jobModalRefreshTimer) {
            window.clearInterval(jobModalRefreshTimer);
            jobModalRefreshTimer = null;
        }
    }
}

async function refreshInstallerStatusOnly() {
    if (installerStatusRefreshActive) return;
    installerStatusRefreshActive = true;
    const installerBox = document.getElementById('installerStatus');
    try {
        const status = await loadJson('install_center.php?action=installer_status');
        installerBox.innerHTML = renderInstallerStatus(status);
        installerBox.classList.remove('skeleton');
    } catch (err) {
        installerBox.innerHTML = `<div class="bad"><i class="fas fa-triangle-exclamation me-1"></i>Installer-Status konnte nicht geladen werden: ${esc(err.message)}</div>`;
        installerBox.classList.remove('skeleton');
    } finally {
        installerStatusRefreshActive = false;
    }
}

async function loadJson(url) {
    const res = await fetch(url, {cache: 'no-store'});
    if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
    return await res.json();
}

async function loadInstallCenter() {
    const root = document.getElementById('moduleRoot');
    const installerBox = document.getElementById('installerStatus');
    root.classList.add('skeleton');
    installerBox.classList.add('skeleton');
    installerBox.innerHTML = '<div class="text-secondary">Lade Installer-Status...</div>';
    root.innerHTML = '<div class="module-grid"><div class="module-card"><div class="text-secondary">Lade Modulkatalog...</div></div></div>';
    try {
        const [status, catalog, diagnosis, services, readiness] = await Promise.all([
            loadJson('install_center.php?action=installer_status'),
            loadJson('install_center.php?action=catalog'),
            loadJson('install_center.php?action=diagnosis'),
            loadJson('service_control.php?action=status_all'),
            loadJson('install_center.php?action=install_module_dry_run').catch(() => ({install_dry_run: {}}))
        ]);
        installerBox.innerHTML = renderInstallerStatus(status);
        const modules = catalog.modules || {};
        const diag = diagnosis.diagnosis || {};
        const serviceMap = services.services || {};
        const readinessMap = readiness.install_dry_run || {};
        installCenterModules = modules;
        installCenterDiagnosis = diag;
        installCenterReadiness = readinessMap;
        const grouped = {};
        Object.values(modules).forEach(module => {
            const group = module.group || 'system';
            if (!grouped[group]) grouped[group] = [];
            grouped[group].push(module);
        });
        let html = '';
        for (const group of ['core', 'consumers', 'integrations', 'system']) {
            if (!grouped[group]) continue;
            const [label, icon] = groupLabels[group] || [group, 'fa-cube'];
            html += `<div class="group-heading"><i class="fas ${icon}"></i>${label}</div><div class="module-grid">`;
            grouped[group].forEach(module => {
                html += renderModule(module, serviceMap[serviceKey(module.service_unit)], diag[module.key], readinessMap[module.key]);
            });
            html += '</div>';
        }
        root.innerHTML = html || '<div class="module-card">Keine Module gefunden.</div>';
    } catch (err) {
        root.innerHTML = `<div class="alert alert-danger">Installationszentrale konnte nicht geladen werden: ${esc(err.message)}</div>`;
        installerBox.innerHTML = `<div class="bad"><i class="fas fa-triangle-exclamation me-1"></i>Installer-Status konnte nicht geladen werden: ${esc(err.message)}</div>`;
    } finally {
        root.classList.remove('skeleton');
        installerBox.classList.remove('skeleton');
    }
}

async function controlService(service, action) {
    const labels = {
        start: 'starten',
        stop: 'stoppen',
        restart: 'neu starten',
        activate_forecast_evidence: 'dauerhaft aktivieren und starten'
    };
    if (!confirm(`Dienst ${service} wirklich ${labels[action] || action}?`)) return;
    const log = document.getElementById('actionLog');
    log.textContent = `Sende ${action} für ${service}...`;
    const body = new FormData();
    body.append('service', service);
    body.append('action', action);
    body.append('csrf_token', serviceControlCsrfToken);
    try {
        const res = await fetch('service_control.php', {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
                'X-CSRF-Token': serviceControlCsrfToken
            },
            body
        });
        let data = null;
        try { data = await res.json(); } catch (_) { data = null; }
        if (!res.ok || !data || data.success !== true) {
            const message = data && (data.message || data.error)
                ? String(data.message || data.error)
                : `HTTP ${res.status}`;
            throw new Error(message);
        }
        log.innerHTML = renderServiceControlResult(data, service, action);
        await loadInstallCenter();
    } catch (err) {
        log.textContent = `Fehler: ${err.message}`;
    }
}

async function loadInstallCenterPvForecastDiagnostics() {
    try {
        const response = await fetch('get_forecast_data.php?t=' + Date.now());
        if (response.ok) {
            const data = await response.json();
            if (typeof updatePvForecastDiagnostics === 'function') {
                updatePvForecastDiagnostics(data);
            }
        }
    } catch (e) {
        console.warn('Prognosediagnose konnte in der Installationszentrale nicht geladen werden:', e);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    loadInstallCenter();
    loadInstallCenterPvForecastDiagnostics();
    window.setInterval(refreshInstallerStatusOnly, 12000);
    window.setInterval(loadInstallCenterPvForecastDiagnostics, 15000);
});
</script>
</body>
</html>
