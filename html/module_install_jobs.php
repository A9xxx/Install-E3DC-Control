<?php
// Gemeinsamer Webeinstieg; privilegierte Prüfungen wiederholt ausschließlich der feste Launcher.
function e3dc_module_launcher_inspect() {
    if (e3dcIsDockerEnvironment()) {
        return ['success' => false, 'reason_code' => 'docker', 'message' => 'Docker-Module werden über Konfiguration und Container-Neustart eingerichtet.'];
    }
    $launcher = '/usr/local/sbin/e3dc-module-installer';
    $contractPath = '/etc/e3dc-control/module_installer_contract.json';
    foreach ([$launcher, $contractPath] as $path) {
        $parent = dirname($path);
        while ($parent !== '/') {
            $meta = @lstat($parent);
            if (!is_array($meta) || ($meta['mode'] & 0170000) !== 0040000 || $meta['uid'] !== 0
                || $meta['gid'] !== 0 || ($meta['mode'] & 0022) !== 0) {
                return ['success' => false, 'reason_code' => 'path_unsafe', 'message' => 'Modul-Launcher liegt nicht in einem root-kontrollierten Systempfad.'];
            }
            $parent = dirname($parent);
        }
        clearstatcache(true, $path);
        $meta = @lstat($path);
        $expectedMode = $path === $launcher ? 0755 : 0644;
        if (!is_array($meta) || ($meta['mode'] & 0170000) !== 0100000 || $meta['uid'] !== 0
            || $meta['gid'] !== 0 || $meta['nlink'] !== 1 || ($meta['mode'] & 07777) !== $expectedMode
            || $meta['size'] <= 0 || $meta['size'] > 1048576) {
            return ['success' => false, 'reason_code' => 'launcher_missing', 'message' => 'Modul-Launcher fehlt oder ist unsicher. Systemabgleich über den administrativen Installer durchführen.'];
        }
    }
    $contract = json_decode((string)@file_get_contents($contractPath), true);
    if (!is_array($contract) || ($contract['schema'] ?? '') !== 'e3dc_module_installer_v1'
        || !preg_match('/^[0-9a-f]{40}$/D', (string)($contract['commit'] ?? ''))
        || !is_array($contract['modules'] ?? null) || empty($contract['modules'])
        || !hash_equals((string)($contract['launcher_sha256'] ?? ''), (string)@hash_file('sha256', $launcher))) {
        return ['success' => false, 'reason_code' => 'commit_unbound', 'message' => 'Modul-Launcher ist nicht an den freigegebenen Commit gebunden. Systemabgleich über den administrativen Installer durchführen.'];
    }
    return ['success' => true, 'contract' => $contract];
}

function e3dc_run_module_launcher($action, $module) {
    requireWebAuth(true);
    if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
        return ['success' => false, 'write_blocked' => true, 'message' => 'Moduländerungen sind ausschließlich per POST erlaubt.'];
    }
    $sent = $_POST['csrf_token'] ?? '';
    if (!is_string($sent) || $sent === '' || !e3dcWebAuthHashEquals(e3dcCsrfToken(), $sent)) {
        return ['success' => false, 'write_blocked' => true, 'message' => 'Sicherheits-Token ungültig. Seite neu laden.'];
    }
    if (!is_string($action) || !in_array($action, ['install', 'remove'], true)
        || !is_string($module) || !preg_match('/^[a-z0-9_]+$/D', $module)) {
        return ['success' => false, 'write_blocked' => true, 'message' => 'Ungültige Modulaktion oder ungültiger Modulschlüssel.'];
    }
    $inspection = e3dc_module_launcher_inspect();
    if (empty($inspection['success'])) return $inspection + ['write_blocked' => true];
    if (!array_key_exists($module, $inspection['contract']['modules'])) {
        return ['success' => false, 'write_blocked' => true, 'message' => 'Modul ist nicht in der root-eigenen Freigabeliste enthalten.'];
    }
    // argv statt Shell: kein Aufruferpfad, keine freien Optionen und keine Umgebungsfreigabe.
    $process = proc_open(['/usr/bin/sudo', '-n', '/usr/local/sbin/e3dc-module-installer', $action, $module],
        [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes, '/', ['PATH' => '/usr/bin:/bin', 'LANG' => 'C.UTF-8']);
    if (!is_resource($process)) {
        return ['success' => false, 'message' => 'Modul-Launcher konnte nicht gestartet werden. sudo-Freigabe administrativ prüfen.'];
    }
    fclose($pipes[0]);
    // Der Launcher bestätigt nur den Hintergrundstart; beide Kanäle gleichzeitig leeren.
    stream_set_blocking($pipes[1], false);
    stream_set_blocking($pipes[2], false);
    $output = '';
    $deadline = microtime(true) + 20.0;
    do {
        $read = array_filter([$pipes[1], $pipes[2]], static fn($pipe) => !feof($pipe));
        if ($read) {
            $write = null; $except = null;
            if (@stream_select($read, $write, $except, 0, 200000) > 0) {
                foreach ($read as $pipe) {
                    $chunk = fread($pipe, 8192);
                    if ($pipe === $pipes[1] && strlen($output) < 1048576) $output .= (string)$chunk;
                }
            }
        }
        $status = proc_get_status($process);
        if (!$status['running'] && feof($pipes[1]) && feof($pipes[2])) break;
    } while (microtime(true) < $deadline);
    if ($status['running']) {
        proc_terminate($process);
        fclose($pipes[1]); fclose($pipes[2]); proc_close($process);
        return ['success' => false, 'reason_code' => 'start_unconfirmed',
                'message' => 'Startantwort nicht bestätigt. Status und Protokoll prüfen; keinen zweiten Auftrag senden.'];
    }
    fclose($pipes[1]); fclose($pipes[2]); proc_close($process);
    $result = json_decode((string)$output, true);
    if (!is_array($result)) {
        return ['success' => false, 'write_blocked' => true, 'reason_code' => 'sudo_or_launcher_failed',
                'message' => 'Modul-Launcher lieferte kein gültiges Ergebnis. sudo-Freigabe und Installationsprotokoll administrativ prüfen.'];
    }
    return $result + ['via_module_launcher' => true];
}


function e3dc_module_job_status($runId) {
    requireWebAuth(true);
    if (!is_string($runId) || !preg_match('/^[0-9a-f-]{36}$/D', $runId)) {
        return ['success' => false, 'message' => 'Ungültige Auftragskennung.'];
    }
    $path = '/run/e3dc-module-installer/status.json';
    $directory = @lstat(dirname($path));
    $meta = @lstat($path);
    if (!is_array($directory) || ($directory['mode'] & 0170000) !== 0040000
        || $directory['uid'] !== 0 || $directory['gid'] !== 0 || ($directory['mode'] & 0022) !== 0
        || !is_array($meta) || ($meta['mode'] & 0170000) !== 0100000 || $meta['uid'] !== 0
        || $meta['gid'] !== 0 || $meta['nlink'] !== 1 || ($meta['mode'] & 07777) !== 0644
        || $meta['size'] > 1048576) {
        return ['success' => false, 'message' => 'Statusdatei fehlt oder ist nicht sicher lesbar.'];
    }
    $result = json_decode((string)@file_get_contents($path), true);
    if (!is_array($result) || ($result['run_id'] ?? '') !== $runId) {
        return ['success' => false, 'reason_code' => 'status_replaced',
                'message' => 'Status gehört zu einem anderen Auftrag. Protokoll prüfen.'];
    }
    return $result;
}

function e3dc_module_jobs_script() {
    return <<<'SCRIPT'
<script>
async function e3dcWaitModuleJob(result, onStatus = () => {}) {
    if (!result || result.state !== 'running') return result;
    const deadline = Date.now() + 30 * 60 * 1000;
    while (Date.now() < deadline) {
        await new Promise(resolve => window.setTimeout(resolve, 1500));
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), 10000);
        try {
            const response = await fetch('install_center.php?action=module_job_status&run_id=' + encodeURIComponent(result.run_id),
                {cache: 'no-store', credentials: 'same-origin', signal: controller.signal});
            if (!response.ok) throw new Error('Statusabfrage fehlgeschlagen: HTTP ' + response.status);
            const status = await response.json();
            if (status.reason_code === 'status_replaced') throw new Error(status.message);
            if (status.run_id !== result.run_id) throw new Error(status.message || 'Auftragsstatus nicht bestätigt.');
            onStatus(status);
            if (status.state !== 'running') return status;
        } finally {
            window.clearTimeout(timeout);
        }
    }
    throw new Error('Statusbeobachtung nach 30 Minuten beendet. Der Auftrag läuft gegebenenfalls weiter; Status und Protokoll prüfen.');
}
</script>
SCRIPT;
}

// Der Plan bleibt in der PIN-geschützten Sitzung. Der Browser erhält nur die öffentliche Ansicht.
function e3dc_setup_catalog() {
    $paths = getInstallPaths();
    $root = !empty($paths['valid']) ? rtrim($paths['install_path'], '/') : '';
    if ($root === '' || !is_file($root . '/Installer/web_installer.py')) return [];
    $process = e3dcRunArgvProcess(['/usr/bin/python3', '-B', $root . '/Installer/web_installer.py', '--action', 'catalog'], 20.0, ['max_output_bytes' => 1024 * 1024]);
    $out = !empty($process['success']) ? ($process['stdout'] ?? '') : '';
    $result = json_decode((string)$out, true);
    return !empty($result['success']) && is_array($result['modules'] ?? null) ? $result['modules'] : [];
}

function e3dc_setup_config_hash() {
    $raw = e3dcReadRegularFileBound('/var/www/html/data/e3dc_v4.json');
    return is_string($raw) ? hash('sha256', $raw) : '';
}

// Dialog und Plan folgen derselben Schutzschicht für Kerndienste.
function e3dc_setup_service_jobs($info, $module) {
    $allowed = [$module['service_unit'] => ['group' => $module['group'] ?? '', 'optional' => $module['optional'] ?? true]];
    $guard = e3dcServiceActionGuard('start', $module['service_unit'], $allowed);
    if (($guard['error_code'] ?? '') === 'core_action_blocked') {
        return empty($info['config_changed']) ? [] : ['restart_service'];
    }
    return $info['jobs'];
}

function e3dc_setup_create_plan($candidates, $expectedHash = null, $catalog = null) {
    if (e3dcIsDockerEnvironment() || !$candidates) return null;
    $hash = e3dc_setup_config_hash();
    if ($expectedHash !== null && !hash_equals((string)$expectedHash, $hash)) return null;
    // Ein im selben Aufruf nach dem Speichern geladener Katalog wird wiederverwendet.
    if (!is_array($catalog) || !$catalog) $catalog = e3dc_setup_catalog();
    if ($hash === '' || !$catalog || !hash_equals($hash, e3dc_setup_config_hash())) return null;
    $steps = []; $visiting = []; $done = [];
    $visit = function($key) use (&$visit, &$steps, &$visiting, &$done, $candidates, $catalog) {
        if (isset($done[$key])) return;
        if (isset($visiting[$key])) throw new RuntimeException('Zyklische Modulabhängigkeit.');
        if (!isset($catalog[$key]) || !is_array($catalog[$key]['setup_dependencies'] ?? null)) {
            throw new RuntimeException('Modulabhängigkeiten nicht verfügbar.');
        }
        $visiting[$key] = true;
        foreach ($catalog[$key]['setup_dependencies'] as $dependency) {
            if (isset($candidates[$dependency]) && empty($candidates[$dependency]['system_update_only'])) $visit($dependency);
        }
        unset($visiting[$key]); $done[$key] = true;
        $info = $candidates[$key];
        $module = $catalog[$key];
        $info['jobs'] = e3dc_setup_service_jobs($info, $module);
        if (!$info['jobs']) return;
        $dependencies = [];
        foreach ($catalog[$key]['setup_dependencies'] as $dependency) {
            if (!isset($catalog[$dependency])) throw new RuntimeException('Abhängigkeit nicht im Dienstkatalog.');
            $dependencies[$dependency] = [
                'label' => $catalog[$dependency]['display_name'],
                'unit' => $catalog[$dependency]['service_unit'],
            ];
        }
        $steps[] = ['module' => $key, 'label' => $catalog[$key]['display_name'],
            'unit' => $catalog[$key]['service_unit'], 'jobs' => $info['jobs'],
            'dependencies' => $dependencies, 'state' => 'pending', 'job_index' => 0, 'message' => 'Ausstehend'];
    };
    try {
        foreach ($candidates as $key => $info) {
            if (empty($info['system_update_only'])) $visit($key);
        }
    } catch (RuntimeException $error) { return null; }
    if (!$steps) return null;
    $token = bin2hex(random_bytes(32));
    $_SESSION['e3dc_setup_plan'] = ['token' => $token, 'expires' => time() + 1800,
        'config_hash' => $hash, 'started' => false, 'complete' => false, 'steps' => $steps];
    return $token;
}

function e3dc_setup_redirect($token) {
    $url = 'install_center.php?setup_token=' . rawurlencode($token);
    if (!headers_sent()) header('Location: ' . $url, true, 303);
    echo '<script>window.top.location.href=' . json_encode($url, JSON_HEX_TAG | JSON_HEX_APOS | JSON_HEX_AMP | JSON_HEX_QUOT) . ';</script>';
    exit;
}

function e3dc_setup_invalid_plan() {
    return ['success' => false, 'message' => 'Einrichtungsplan abgelaufen – Konfiguration erneut speichern oder Module einzeln einrichten'];
}

function e3dc_setup_public_plan($plan) {
    unset($plan['config_hash']);
    return $plan;
}

function e3dc_setup_load_plan($token) {
    $plan = $_SESSION['e3dc_setup_plan'] ?? null;
    if (!is_string($token) || !preg_match('/^[0-9a-f]{64}$/D', $token)
        || !is_array($plan) || !hash_equals((string)$plan['token'], $token)
        || (!$plan['started'] && (int)$plan['expires'] < time()) || e3dcIsDockerEnvironment()) return e3dc_setup_invalid_plan();
    if (!hash_equals((string)$plan['config_hash'], e3dc_setup_config_hash())) {
        return ['success' => false, 'message' => 'Konfiguration wurde geändert – Einrichtung angehalten. Konfiguration erneut speichern.'];
    }
    return ['success' => true, 'plan' => $plan];
}

function e3dc_setup_read_plan($token) {
    $result = e3dc_setup_load_plan($token);
    if (!empty($result['success'])) $result['plan'] = e3dc_setup_public_plan($result['plan']);
    return $result;
}

function e3dc_setup_run_job($step, $job) {
    $actions = ['enable_service' => 'enable', 'start_service' => 'start', 'restart_service' => 'restart'];
    if ($step['module'] === 'forecast_evidence') return e3dcActivateForecastEvidenceService();
    if ($job === 'install_module') return e3dc_run_module_launcher('install', $step['module']);
    return isset($actions[$job]) ? e3dcControlSystemdService($actions[$job], $step['unit'])
        : ['success' => false, 'message' => 'Ungültige Modulaktion.'];
}

function e3dc_setup_plan_action($action, $token) {
    requireWebAuth(true);
    if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST'
        || !is_string($_POST['csrf_token'] ?? null)
        || !e3dcWebAuthHashEquals(e3dcCsrfToken(), $_POST['csrf_token'])) {
        return ['success' => false, 'message' => 'Sicherheits-Token ungültig. Seite neu laden.'];
    }
    // PHP hält den exklusiven Sitzungsschreiblock bis zum Requestende.
    // Ohne aktiven Lock darf kein Schritt einen Launcher starten.
    if (session_status() !== PHP_SESSION_ACTIVE) return ['success' => false, 'message' => 'Sitzung nicht verfügbar. Seite neu laden.'];
    $result = e3dc_setup_load_plan($token);
    if (empty($result['success'])) return $result;
    $plan = $result['plan'];
    // Ein alter Plan (letzter Schritt vor mehr als 30 Minuten) läuft nur nach „Einrichtung fortsetzen“ weiter,
    // auch wenn ein aufgewachter Tab noch Schritte sendet.
    if ($action === 'step' && $plan['started'] && !$plan['complete'] && empty($_POST['resume'])
        && time() - (int)($plan['last_activity'] ?? 0) > 1800) {
        return ['success' => false, 'stale' => true, 'plan' => e3dc_setup_public_plan($plan),
            'message' => 'Einrichtung angehalten: Der letzte Schritt liegt mehr als 30 Minuten zurück. Zum Weitermachen „Einrichtung fortsetzen“ wählen.'];
    }
    if ($action === 'start') {
        // Einmal-Token wird vor dem ersten Launcher-Aufruf verbraucht.
        if ($plan['started']) return ['success' => true, 'plan' => e3dc_setup_public_plan($plan)];
        $plan['started'] = true;
    } elseif ($action === 'retry') {
        if (!$plan['started'] || !$plan['complete']) return ['success' => false, 'message' => 'Laufenden Einrichtungsplan zuerst abschließen.'];
        foreach ($plan['steps'] as &$step) {
            if (in_array($step['state'], ['failed', 'blocked', 'skipped'], true)) {
                $step['state'] = 'pending'; $step['job_index'] = 0; $step['message'] = 'Erneuter Versuch ausstehend';
                unset($step['wait_since'], $step['next_check'], $step['run_id']);
            }
        }
        unset($step); $plan['complete'] = false;
    } elseif ($action !== 'step' || !$plan['started']) {
        return ['success' => false, 'message' => 'Einrichtungsplan wurde nicht gestartet.'];
    } elseif (!$plan['complete']) {
        foreach ($plan['steps'] as &$step) {
            if (!in_array($step['state'], ['pending', 'waiting', 'running'], true)) continue;
            if ($step['state'] === 'running') {
                $job = e3dc_module_job_status($step['run_id']);
                if (($job['state'] ?? '') === 'running') break;
                unset($step['run_id']);
                if (empty($job['success'])) {
                    $step['state'] = 'failed'; $step['message'] = $job['message'] ?? 'Modulauftrag nicht bestätigt.';
                    break;
                }
                $step['message'] = $job['message'] ?? 'Modulauftrag abgeschlossen.';
                $step['job_index']++; $step['state'] = 'pending';
            }
            $blocked = null;
            foreach ($step['dependencies'] as $key => $dependency) {
                foreach ($plan['steps'] as $previous) {
                    if ($previous['module'] === $key && in_array($previous['state'], ['failed', 'blocked', 'skipped'], true)) {
                        $blocked = 'übersprungen: Abhängigkeit ' . $dependency['label'] . ' nicht aktiv';
                        $step['state'] = 'skipped'; break 2;
                    }
                }
                if (isset($step['next_check']) && time() < $step['next_check']) { $blocked = $step['message']; break; }
                $state = e3dc_service_unit_state($dependency['unit']);
                if (!$state['exists'] || !$state['active']) {
                    $blocked = !$state['exists']
                        ? 'Abhängigkeit ' . $dependency['label'] . ' ist nicht installiert. Zuerst in der Installationszentrale installieren.'
                        : 'Abhängigkeit ' . $dependency['label'] . ' nicht aktiv';
                    $step['wait_since'] ??= time();
                    if ($state['exists'] && time() - $step['wait_since'] < 60) {
                        $step['state'] = 'waiting'; $step['next_check'] = time() + 5;
                        $blocked = 'Warten auf aktive Abhängigkeit: ' . $dependency['label'];
                    } else $step['state'] = 'blocked';
                    break;
                }
            }
            if ($blocked !== null) { $step['message'] = $blocked; break; }
            unset($step['wait_since'], $step['next_check']);
            $step['state'] = 'pending';
            if ($step['job_index'] < count($step['jobs'])) {
                $job = e3dc_setup_run_job($step, $step['jobs'][$step['job_index']]);
                $step['message'] = $job['message'] ?? (!empty($job['success']) ? 'Modulauftrag abgeschlossen.' : 'Modulauftrag fehlgeschlagen.');
                if (($job['state'] ?? '') === 'running' && !empty($job['run_id'])) {
                    $step['state'] = 'running'; $step['run_id'] = $job['run_id'];
                } elseif (empty($job['success'])) $step['state'] = 'failed';
                else $step['job_index']++;
            }
            if ($step['state'] === 'pending' && $step['job_index'] >= count($step['jobs'])) {
                $step['state'] = 'done';
            }
            break;
        }
        unset($step);
    }
    $plan['complete'] = !array_filter($plan['steps'], static fn($step) => in_array($step['state'], ['pending', 'waiting', 'running'], true));
    $errors = count(array_filter($plan['steps'], static fn($step) => in_array($step['state'], ['failed', 'blocked', 'skipped'], true)));
    // Zeitpunkt des letzten Schritts: Ohne Link setzt die Installationszentrale nur kurz zuvor bearbeitete Pläne fort.
    $plan['last_activity'] = time();
    $plan['message'] = $plan['complete'] ? ($errors ? 'Einrichtung abgeschlossen; ' . $errors . ($errors === 1 ? ' Schritt nicht eingerichtet.' : ' Schritte nicht eingerichtet.') : 'Einrichtung abgeschlossen.') : 'Einrichtung läuft.';
    $_SESSION['e3dc_setup_plan'] = $plan;
    return ['success' => true, 'plan' => e3dc_setup_public_plan($plan)];
}
