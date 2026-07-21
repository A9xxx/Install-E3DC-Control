<?php
session_start();
require_once 'helpers.php';

$v4_config_file_path = '/var/www/html/data/e3dc_v4.json';
$wizardPathContext = getInstallPaths();

// Wenn Config existiert, wurde Setup bereits abgeschlossen
if (file_exists($v4_config_file_path)) {
    header("Location: index.php");
    exit;
}

$step = isset($_GET['step']) ? (int)$_GET['step'] : 1;

// Sicherstellen, dass var/www/html/data existiert
$data_dir = dirname($v4_config_file_path);
if (!file_exists($data_dir)) {
    @mkdir($data_dir, 0775, true);
    // Rechte reparieren falls möglich
    $wizardOwner = (string)($wizardPathContext['install_user'] ?? '');
    if ($wizardOwner !== '' && function_exists('posix_getpwnam')) {
        $wizardAccount = @posix_getpwnam($wizardOwner);
        if (is_array($wizardAccount)) @chown($data_dir, $wizardAccount['uid']);
    }
    if (function_exists('posix_getgrnam')) @chgrp($data_dir, posix_getgrnam('www-data')['gid']);
}

// Formulardaten in Session sammeln. Ohne eindeutigen Installationskontext
// werden weder Konfiguration noch Dienste verändert.
if ($_SERVER['REQUEST_METHOD'] === 'POST' && empty($wizardPathContext['valid'])) {
    http_response_code(503);
    $error = $wizardPathContext['error'] ?? 'Installationskontext fehlt.';
} elseif ($_SERVER['REQUEST_METHOD'] === 'POST') {
    if (isset($_POST['wizard_data'])) {
        foreach ($_POST['wizard_data'] as $key => $value) {
            $_SESSION['wizard_data'][$key] = trim($value);
        }
    }

    if (isset($_POST['next'])) {
        // Validation for step 2 (E3DC IP)
        if ($step === 2 && empty($_SESSION['wizard_data']['server_ip'])) {
            $error = "IP-Adresse des Hauskraftwerks darf nicht leer sein!";
        } else {
            $step++;
            header("Location: install_wizard.php?step=$step");
            exit;
        }
    } elseif (isset($_POST['prev'])) {
        $step--;
        header("Location: install_wizard.php?step=$step");
        exit;
    } elseif (isset($_POST['finish'])) {
        // Finale Konfiguration speichern
        $defaults = [
            "server_port" => "5033",
            "wurzelzaehler" => "0",
            "wurzelzaehler_invertiert" => "0",
            "check_updates" => "1",
            "darkmode" => "1",
            "show_forecast" => "1",
            "einspeiselimit" => "7.0",
            "speichergroesse" => "15",
            "auto_mode" => "1",
            "wb_native_type" => "e3dc_auto",
            "wb1_e3dc_wbchar6_compat_enable" => "1",
            "config_secret_protection_mode" => "standard"
        ];

        $final_data = array_merge($defaults, $_SESSION['wizard_data'] ?? []);

        // Typkonvertierung von Zahlen
        foreach ($final_data as $k => $v) {
            if (is_numeric($v)) {
                $final_data[$k] = (strpos($v, '.') !== false) ? floatval($v) : intval($v);
            }
        }

        // JSON Speichern
        $json_content = json_encode($final_data, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE);

        if (file_put_contents($v4_config_file_path, $json_content)) {
            // Sudo chmod für www-data
            $wizardPaths = getInstallPaths();
            $wizardInstallUser = !empty($wizardPaths['valid'])
                ? preg_replace('/[^A-Za-z0-9_.-]/', '', (string)$wizardPaths['install_user'])
                : '';
            $wizardFileMode = sprintf('%o', e3dcConfigSecretFileModeFromData($final_data));
            if ($wizardInstallUser !== '') {
                exec("sudo chown " . escapeshellarg($wizardInstallUser . ":www-data") . " " . escapeshellarg($v4_config_file_path) . " && sudo chmod " . escapeshellarg($wizardFileMode) . " " . escapeshellarg($v4_config_file_path));
            }

            // Jetzt die essenziellen Dienste starten (falls der Wrapper existiert)
            $wrapper_path = $wizardInstallUser !== '' ? (e3dcFindServiceWrapper() ?: '') : '';
            if(file_exists($wrapper_path)) {
                exec("sudo $wrapper_path restart e3dc-live");
                exec("sudo $wrapper_path restart e3dc-weather-manager");
                exec("sudo $wrapper_path restart e3dc-epex-manager");
                exec("sudo $wrapper_path restart e3dc-storage-simulator");
            }

            // Aufräumen und zum Dashboard springen
            unset($_SESSION['wizard_data']);
            header("Location: index.php?setup=success");
            exit;
        } else {
            $error = "✗ Konnte Konfiguration nicht speichern. Bitte Berechtigungen (`/var/www/html/data`) prüfen.";
        }
    }
}

$data = $_SESSION['wizard_data'] ?? [];
?>
<!DOCTYPE html>
<html lang="de" data-bs-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>E3DC-Control V4 Setup</title>
    <link href="assets/vendor/bootstrap/css/bootstrap.min.css" rel="stylesheet">
    <link href="assets/vendor/fontawesome/css/all.min.css" rel="stylesheet">
    <style>
        body { background-color: #121212; color: #e0e0e0; font-family: 'Segoe UI', Tahoma, Verdana, sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
        .wizard-card { background-color: #1e1e1e; border-radius: 16px; border: 1px solid #333; box-shadow: 0 10px 40px rgba(0,0,0,0.5); overflow: hidden; width: 100%; max-width: 650px; }
        .wizard-header { background: linear-gradient(135deg, #0f172a 0%, #1e1b4b 100%); padding: 25px 30px; border-bottom: 1px solid #333; }
        .wizard-body { padding: 30px; }
        .wizard-footer { padding: 15px 30px; border-top: 1px solid #333; background: rgba(0,0,0,0.2); display: flex; justify-content: space-between; }
        .step-indicators { display: flex; gap: 8px; margin-top: 15px; }
        .step-dot { height: 6px; flex: 1; background: #333; border-radius: 3px; }
        .step-dot.active { background: #38bdf8; box-shadow: 0 0 10px rgba(56, 189, 248, 0.5); }
        .step-dot.completed { background: #10b981; }
        .form-floating>label { color: #888; }
        .form-control, .form-select { background-color: #2d2d2d; border: 1px solid #444; color: #fff; }
        .form-control:focus, .form-select:focus { background-color: #2a2a2a; color: #fff; border-color: #38bdf8; box-shadow: 0 0 0 0.25rem rgba(56, 189, 248, 0.25); }
    </style>
</head>
<body>

<div class="wizard-card mx-3 my-4">
    <div class="wizard-header">
        <h3 class="mb-0 fw-bold text-white d-flex align-items-center">
            <i class="fas fa-solar-panel text-warning me-3"></i> E3DC-Control Ersteinrichtung
        </h3>
        <div class="step-indicators">
            <?php for($i=1; $i<=4; $i++): ?>
                <div class="step-dot <?= $i < $step ? 'completed' : ($i === $step ? 'active' : '') ?>"></div>
            <?php endfor; ?>
        </div>
    </div>

    <form method="POST">
        <div class="wizard-body">
            <?php if(isset($error)): ?>
                <div class="alert alert-danger"><i class="fas fa-exclamation-triangle me-2"></i><?= htmlspecialchars($error) ?></div>
            <?php endif; ?>

            <?php if($step === 1): ?>
                <!-- STEP 1: Willkommen -->
                <div class="text-center mb-4">
                    <i class="fas fa-magic fa-3x text-primary mb-3"></i>
                    <h4>Willkommen zur V4!</h4>
                    <p class="text-muted">Willkommen beim neuen, nativen und autonomen Energiemanagement. Bevor es losgeht, benötigen wir einige Basisinformationen.</p>
                </div>

                <h6 class="text-info fw-bold mb-3 border-bottom border-secondary pb-2">Allgemeines</h6>

                <div class="form-floating mb-3">
                    <input type="password" class="form-control" id="web_pin" name="wizard_data[web_pin]" placeholder="PIN (Optional)" value="<?= htmlspecialchars($data['web_pin'] ?? '') ?>">
                    <label for="web_pin">Schutz-PIN für dieses Web-Dashboard (Optional)</label>
                </div>

                <div class="row g-2 mb-3">
                    <div class="col-6">
                        <label class="form-label small text-muted mb-1">Einspeiselimit (kW)</label>
                        <input type="number" step="0.1" class="form-control" name="wizard_data[einspeiselimit]" placeholder="z.b. 7.0" value="<?= htmlspecialchars($data['einspeiselimit'] ?? '') ?>">
                    </div>
                    <div class="col-6">
                        <label class="form-label small text-muted mb-1">Max. Netz-Bezug (kW)</label>
                        <input type="number" step="0.1" class="form-control" name="wizard_data[maximumladeleistung]" placeholder="z.b. 12.5" value="<?= htmlspecialchars($data['maximumladeleistung'] ?? '') ?>">
                    </div>
                </div>

            <?php elseif($step === 2): ?>
                <!-- STEP 2: E3DC Verbindung -->
                <div class="text-center mb-4">
                    <i class="fas fa-server fa-3x text-warning mb-3"></i>
                    <h4>Hauskraftwerk Verbindung</h4>
                    <p class="text-muted">Damit wir das Hauskraftwerk live steuern können, benötigen wir die lokalen Zugangsdaten (RSCP).</p>
                </div>

                <div class="form-floating mb-3">
                        <input type="text" class="form-control" id="server_ip" name="wizard_data[server_ip]" placeholder="192.0.2.50" value="<?= htmlspecialchars($data['server_ip'] ?? '') ?>" required>
                        <label for="server_ip">IP-Adresse d. E3DC (z.B. 192.0.2.50) *</label>
                </div>

                <div class="form-floating mb-3">
                    <input type="text" class="form-control" id="e3dc_user" name="wizard_data[e3dc_user]" placeholder="Email" value="<?= htmlspecialchars($data['e3dc_user'] ?? '') ?>" required>
                    <label for="e3dc_user">E3DC Portal Benutzername (Email) *</label>
                </div>

                <div class="row g-2 mb-3">
                    <div class="col-6">
                        <div class="form-floating">
                            <input type="password" class="form-control" id="e3dc_password" name="wizard_data[e3dc_password]" placeholder="***" value="<?= htmlspecialchars($data['e3dc_password'] ?? '') ?>" required>
                            <label for="e3dc_password">Portal Passwort *</label>
                        </div>
                    </div>
                    <div class="col-6">
                        <div class="form-floating">
                            <input type="password" class="form-control" id="aes_password" name="wizard_data[aes_password]" placeholder="***" value="<?= htmlspecialchars($data['aes_password'] ?? '') ?>" required>
                            <label for="aes_password">RSCP AES Kennwort *</label>
                        </div>
                    </div>
                </div>

            <?php elseif($step === 3): ?>
                <!-- STEP 3: Prognose & Markt -->
                <div class="text-center mb-4">
                    <i class="fas fa-cloud-sun fa-3x text-info mb-3"></i>
                    <h4>Wetter & Märkte</h4>
                    <p class="text-muted">Für Ladekurve, Pre-Dump, Quell-Erholung und Peak-Shaving.</p>
                </div>

                <h6 class="text-info fw-bold mb-3 border-bottom border-secondary pb-2">PV-Prognose (Open-Meteo KI)</h6>
                <div class="row g-2 mb-3">
                    <div class="col-6">
                        <label class="form-label small text-muted mb-1">Breitengrad (Latitude)</label>
                        <input type="text" class="form-control" name="wizard_data[hoehe]" placeholder="z.b. 51.163" value="<?= htmlspecialchars($data['hoehe'] ?? '') ?>" required>
                    </div>
                    <div class="col-6">
                        <label class="form-label small text-muted mb-1">Längengrad (Longitude)</label>
                        <input type="text" class="form-control" name="wizard_data[laenge]" placeholder="z.b. 10.448" value="<?= htmlspecialchars($data['laenge'] ?? '') ?>" required>
                    </div>
                </div>

                <div class="form-floating mb-4">
                    <input type="text" class="form-control" id="forecast1" name="wizard_data[forecast1]" placeholder="35/0/10.0" value="<?= htmlspecialchars($data['forecast1'] ?? '35/0/10.0') ?>" required>
                    <label for="forecast1">String 1: Neigung / Ausrichtung / kWp</label>
                    <div class="form-text mt-1 text-muted">Ausrichtung: Süd=0, Ost=-90, West=+90. Beispiel: 35/0/10.0</div>
                </div>

                <h6 class="text-success fw-bold mb-3 border-bottom border-secondary pb-2">Dynamischer Stromtarif</h6>
                <div class="row g-2 mb-3">
                    <div class="col-6">
                        <label class="form-label small text-muted mb-1">Anbieter</label>
                        <select class="form-select" name="wizard_data[tariff_provider]">
                            <option value="smard" <?= ($data['tariff_provider'] ?? '') == 'smard' ? 'selected' : '' ?>>Keiner / SMARD Referenz</option>
                            <option value="entsoe" <?= ($data['tariff_provider'] ?? '') == 'entsoe' ? 'selected' : '' ?>>ENTSO-E API</option>
                            <option value="tibber" <?= ($data['tariff_provider'] ?? '') == 'tibber' ? 'selected' : '' ?>>Tibber</option>
                            <option value="awattar" <?= ($data['tariff_provider'] ?? '') == 'awattar' ? 'selected' : '' ?>>aWATTar</option>
                            <option value="octopus" <?= ($data['tariff_provider'] ?? '') == 'octopus' ? 'selected' : '' ?>>Octopus Energy</option>
                        </select>
                    </div>
                    <div class="col-6">
                        <label class="form-label small text-muted mb-1">Basispreis (Fixkosten Anteil)</label>
                        <input type="number" step="0.1" class="form-control" name="wizard_data[strompreis_basis]" placeholder="z.b. 18.5" value="<?= htmlspecialchars($data['strompreis_basis'] ?? '0.0') ?>">
                    </div>
                </div>

            <?php elseif($step === 4): ?>
                <!-- STEP 4: Smart Home Hardware -->
                <div class="text-center mb-4">
                    <i class="fas fa-home fa-3x text-success mb-3"></i>
                    <h4>Smart Home Integration</h4>
                    <p class="text-muted">Wähle aus, welche Erweiterungen vom Energy-Manager gesteuert werden sollen.</p>
                </div>

                <div class="card bg-body-tertiary border-secondary mb-3">
                    <div class="card-body">
                        <div class="form-check form-switch mb-0 fs-5 d-flex align-items-center">
                            <input class="form-check-input me-3" type="checkbox" role="switch" id="wb_native_enable" name="wizard_data[wb_native_enable]" value="1" <?= ($data['wb_native_enable'] ?? '') == '1' ? 'checked' : '' ?> onchange="document.getElementById('wallbox_backend_details').style.display = this.checked ? 'block' : 'none'">
                            <label class="form-check-label fw-bold" for="wb_native_enable"><i class="fas fa-charging-station text-warning me-2"></i>Wallbox-Regelung (E3/DC efy/Easy, openWB, go-e)</label>
                        </div>
                        <div id="wallbox_backend_details" class="mt-3 border-top border-secondary pt-3" style="display: <?= ($data['wb_native_enable'] ?? '') == '1' ? 'block' : 'none' ?>;">
                            <label class="form-label small">Wallbox-Modell / API</label>
                            <select class="form-select mb-3" name="wizard_data[wb_native_type]" id="wizard_wb_native_type">
                                <?php $wizardWbType = (string)($data['wb_native_type'] ?? 'e3dc_auto'); ?>
                                <option value="e3dc_auto" <?= $wizardWbType === 'e3dc_auto' ? 'selected' : '' ?>>E3/DC automatisch (efy / Easy Connect / Multi Connect)</option>
                                <option value="e3dc_efy" <?= $wizardWbType === 'e3dc_efy' ? 'selected' : '' ?>>E3/DC Wallbox efy</option>
                                <option value="e3dc_easy_connect" <?= $wizardWbType === 'e3dc_easy_connect' ? 'selected' : '' ?>>E3/DC Easy Connect</option>
                                <option value="e3dc_multi" <?= $wizardWbType === 'e3dc_multi' ? 'selected' : '' ?>>E3/DC Multi Connect</option>
                                <option value="openwb" <?= $wizardWbType === 'openwb' ? 'selected' : '' ?>>openWB Controller</option>
                                <option value="openwb_pro" <?= $wizardWbType === 'openwb_pro' ? 'selected' : '' ?>>openWB Pro</option>
                                <option value="go-e" <?= $wizardWbType === 'go-e' ? 'selected' : '' ?>>go-eCharger</option>
                            </select>
                            <label class="form-label small">E3/DC-Regelbackend</label>
                            <?php $wizardWbCompat = (string)($data['wb1_e3dc_wbchar6_compat_enable'] ?? '1'); ?>
                            <select class="form-select" name="wizard_data[wb1_e3dc_wbchar6_compat_enable]">
                                <option value="1" <?= $wizardWbCompat !== '0' ? 'selected' : '' ?>>Empfohlen: WBchar6-Kompatibilit&auml;tsregelung f&uuml;r Modus und Strom</option>
                                <option value="0" <?= $wizardWbCompat === '0' ? 'selected' : '' ?>>Nur Status – keine E3/DC-Regelbefehle</option>
                            </select>
                            <div class="small text-muted mt-2">Direkte Sun-/Auto-/Abort-, Maximalstrom- und native E3/DC-Phasenbefehle sind nicht freigegeben.</div>
                        </div>
                    </div>
                </div>

                <div class="card bg-body-tertiary border-secondary mb-3">
                    <div class="card-body">
                        <div class="form-check form-switch mb-2 fs-5 d-flex align-items-center">
                            <input class="form-check-input me-3" type="checkbox" role="switch" id="luxtronik" name="wizard_data[luxtronik]" value="1" <?= ($data['luxtronik'] ?? '') == '1' ? 'checked' : '' ?> onchange="document.getElementById('wp_details').style.display = this.checked ? 'block' : 'none'">
                            <label class="form-check-label fw-bold" for="luxtronik"><i class="fas fa-fire-burner text-danger me-2"></i>SG-Ready Wärmepumpe</label>
                        </div>
                        <div id="wp_details" style="display: <?= ($data['luxtronik'] ?? '') == '1' ? 'block' : 'none' ?>;" class="mt-3 border-top border-secondary pt-3">
                            <div class="mb-3">
                                <label class="form-label small">Steuerungs-Typ</label>
                                <select class="form-select" name="wizard_data[wp_type]">
                                    <option value="-1" <?= ($data['wp_type'] ?? '') == '-1' ? 'selected' : '' ?>>Keine WÃ¤rmepumpe</option>
                                    <option value="0" <?= ($data['wp_type'] ?? '') == '0' ? 'selected' : '' ?>>Luxtronik (Alpha-Innotec / Novelan)</option>
                                    <option value="1" <?= ($data['wp_type'] ?? '') == '1' ? 'selected' : '' ?>>IDM Navigator 2.0 (Modbus-TCP)</option>
                                    <option value="2" <?= ($data['wp_type'] ?? '') == '2' ? 'selected' : '' ?>>Direkter Heizstab / Shelly</option>
                                    <option value="3" <?= ($data['wp_type'] ?? '') == '3' ? 'selected' : '' ?>>Shelly Pro3EM (WP-Messung)</option>
                                </select>
                            </div>
                        </div>
                    </div>
                </div>

                <div class="card bg-body-tertiary border-secondary mb-3">
                    <div class="card-body">
                        <div class="form-check form-switch mb-0 fs-5 d-flex align-items-center">
                            <input class="form-check-input me-3" type="checkbox" role="switch" id="matter_bridge" name="wizard_data[matter_bridge]" value="1" <?= ($data['matter_bridge'] ?? '') == '1' ? 'checked' : '' ?>>
                            <label class="form-check-label fw-bold" for="matter_bridge"><i class="fas fa-house-signal text-primary me-2"></i>Apple Home / Matter Integration</label>
                        </div>
                        <div class="small text-muted mt-2">Lokale read-only Statusschalter für Wallbox, PV-Produktion und Netzeinspeisung.</div>
                    </div>
                </div>

            <?php endif; ?>
        </div>

        <div class="wizard-footer">
            <?php if($step > 1): ?>
                <button type="submit" name="prev" class="btn btn-outline-secondary px-4"><i class="fas fa-chevron-left me-2"></i>Zurück</button>
            <?php else: ?>
                <div></div>
            <?php endif; ?>

            <?php if($step < 4): ?>
                <button type="submit" name="next" class="btn btn-primary px-4 fw-bold shadow-sm">Weiter<i class="fas fa-chevron-right ms-2"></i></button>
            <?php else: ?>
                <button type="submit" name="finish" class="btn btn-success px-4 fw-bold shadow-lg"><i class="fas fa-rocket me-2"></i>Speichern & Starten</button>
            <?php endif; ?>
        </div>
    </form>
</div>

</body>
</html>
