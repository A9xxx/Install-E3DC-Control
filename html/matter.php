<?php
$title = "Matter Bridge Integration";
require_once 'helpers.php';
requireWebAuth(false);
$siteName = "Smart Home (Matter)";
$resetNotice = null;
$resetOk = null;

function matterServiceInstalled() {
    if (e3dcIsDockerEnvironment()) {
        $matterDir = '/app/pi/Install/Installer/matter';
        return is_file($matterDir . '/package.json')
            && is_file($matterDir . '/package-lock.json')
            && is_file($matterDir . '/matter_bridge.js');
    }
    exec('/bin/systemctl list-unit-files e3dc-matter-bridge.service --no-legend 2>/dev/null', $out, $code);
    return $code === 0 && !empty($out);
}

function matterServiceActive() {
    if (e3dcIsDockerEnvironment()) {
        // Docker besitzt keinen belastbaren systemd-Dienststatus. Der Worker
        // wird ausschließlich durch PID 1 des Containers verwaltet.
        return null;
    }
    exec('/bin/systemctl is-active e3dc-matter-bridge.service 2>/dev/null', $out, $code);
    return $code === 0 && trim(implode("\n", $out)) === 'active';
}

// Pairing Reset Handle
if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_POST['reset_pairing'])) {
    e3dcRequireCsrfToken(false);
    $reset = e3dcResetMatterPairing();
    $resetOk = !empty($reset['success']);
    if (!$resetOk) {
        $resetNotice = (string)($reset['message'] ?? 'Matter-Kopplung konnte nicht sicher zurückgesetzt werden.');
    } elseif (!empty($reset['restart_queued'])) {
        $resetNotice = "Matter-Reset wurde sicher angefordert. Der Container startet neu und erzeugt danach einen neuen Kopplungscode.";
    } elseif (!empty($reset['service_missing'])) {
        $resetNotice = "Matter-Kopplungsdaten wurden sicher gelöscht. Der Matter-Dienst ist nicht installiert und wurde deshalb nicht gestartet.";
    } elseif (!empty($reset['service_preserved_inactive'])) {
        $resetNotice = "Matter-Kopplungsdaten wurden sicher gelöscht. Der zuvor gestoppte Matter-Dienst bleibt wie gewünscht gestoppt.";
    } else {
        $resetNotice = "Matter-Kopplung wurde sicher zurückgesetzt. Der stabil bestätigte Dienststart erzeugt gleich einen neuen Code.";
    }
    if ($resetOk && !empty($reset['storage_quarantined'])) {
        $resetNotice .= ' Der alte Matter-Storage wurde in der privaten Resettransaktion isoliert, descriptorgebunden geleert und die Quarantäne anschließend vollständig entfernt.';
    }
}

if ($resetNotice === null && e3dcIsDockerEnvironment()) {
    $dockerResetStatus = e3dcMatterResetDockerStatus();
    if (in_array($dockerResetStatus, ['MATTER_RESET_OK_DOCKER', 'MATTER_RESET_OK_DOCKER_QUARANTINED'], true)) {
        $resetOk = true;
        $resetNotice = 'Matter-Kopplungsdaten wurden vor dem Workerstart sicher gelöscht. Die Matter Bridge darf mit einem neuen Kopplungscode starten.';
        if ($dockerResetStatus === 'MATTER_RESET_OK_DOCKER_QUARANTINED') {
            $resetNotice .= ' Der alte Matter-Storage wurde isoliert, descriptorgebunden geleert und die private Quarantäne vollständig entfernt.';
        }
    } elseif (str_starts_with($dockerResetStatus, 'MATTER_RESET_ERROR_DOCKER_')) {
        $resetOk = false;
        $resetNotice = e3dcMatterResetFailureMessage($dockerResetStatus);
    }
}

$matterDocker = e3dcIsDockerEnvironment();
$matterInstalled = matterServiceInstalled();
$matterActive = matterServiceActive();

// Pairing Datei lesen (wird vom matter_bridge.js Knoten geschrieben)
$pairingFile = '/var/www/html/ramdisk/matter_pairing.json';

$pairingNamed = @lstat($pairingFile);
$pairingRaw = e3dcReadRegularFileBound($pairingFile, 16384);
$pairingData = e3dcNormalizeMatterPairingPayload($pairingRaw);
$pairingDiagnostic = (string)($pairingData['diagnostic'] ?? '');
if (!is_array($pairingNamed) && $pairingRaw === null) {
    // Eine noch nicht erzeugte Datei ist beim Dienststart ein normaler Zustand.
    $pairingDiagnostic = '';
}
?>
<!-- matter.php wird nun direkt ins Dashboard eingebettet -->
<style>
    .matter-logo { max-width: 150px; margin-bottom: 20px; }
    .qr-placeholder { background: white; padding: 20px; border-radius: 10px; display: inline-block; }
</style>

    <div class="container mt-4">
        <h2 class="mb-4"><i class="fas fa-home me-2"></i><?= $siteName ?></h2>

        <div class="row">
            <div class="col-md-6 mb-4">
                <div class="card shadow-sm h-100">
                    <div class="card-header bg-primary text-body">
                        <h5 class="mb-0"><i class="fas fa-mobile-screen-button me-2"></i>Apple Home / Google Home Pairing</h5>
                    </div>
                    <div class="card-body text-center p-5">
                        <?php if ($resetNotice): ?>
                            <div class="alert <?= $resetOk ? 'alert-success' : 'alert-warning' ?> mb-4">
                                <i class="fas <?= $resetOk ? 'fa-check-circle' : 'fa-triangle-exclamation' ?> me-2"></i><?= htmlspecialchars($resetNotice) ?>
                            </div>
                        <?php endif; ?>

                        <?php if ($pairingDiagnostic !== ''): ?>
                            <div class="alert alert-secondary mb-4">
                                <i class="fas fa-circle-info me-2"></i>Die lokale Matter-Statusdatei ist derzeit nicht sicher lesbar. Die Seite zeigt deshalb vorsorglich „nicht gekoppelt“ an.
                            </div>
                        <?php endif; ?>

                        <?php if (!$matterInstalled): ?>
                            <div class="alert alert-danger mb-4">
                                <i class="fas fa-plug-circle-xmark me-2"></i>Der Matter-Dienst ist noch nicht installiert. Bitte die Matter-Bridge einmal im Installer oder über die Service-Installation einrichten.
                            </div>
                        <?php elseif (!$matterDocker && !$matterActive): ?>
                            <div class="alert alert-warning mb-4">
                                <i class="fas fa-pause me-2"></i>Der Matter-Dienst ist installiert, läuft aber gerade nicht. Beim Zurücksetzen bleibt dieser gewünschte Stoppzustand erhalten.
                            </div>
                        <?php elseif ($matterDocker): ?>
                            <div class="alert alert-info mb-4">
                                <i class="fas fa-cube me-2"></i>Die Matter Bridge wird in Docker durch den Container verwaltet. Ein systemd-Status aus dem Container wird deshalb bewusst nicht angezeigt.
                            </div>
                        <?php endif; ?>

                        <?php if ($pairingData['isCommissioned']): ?>
                            <div class="text-success mb-4 mt-3">
                                <i class="fas fa-check-circle fa-4x mb-3"></i>
                                <h4>Bridge erfolgreich gekoppelt!</h4>
                                <p class="text-muted">Das System ist bereits aktiv in Ihrem Matter-Netzwerk eingebunden.</p>
                            </div>
                        <?php else: ?>
                            <div class="text-muted mb-3">
                                Verwende den lokalen manuellen Code in Apple Home, Google Home oder einem anderen Matter-Controller, um das "E3DC Hauskraftwerk" hinzuzufügen.
                            </div>

                            <?php if (!empty($pairingData['manual'])): ?>
                                <div class="alert alert-info mb-3 text-start">
                                    <i class="fas fa-shield-halved me-2"></i>Der Pairing-Code wird aus Datenschutzgründen nicht an einen externen QR-Dienst übertragen. Bitte verwenden Sie den manuellen Code in Ihrer Home-App.
                                </div>

                                <div class="mt-2 p-2 bg-body-tertiary rounded">
                                    <div class="small text-muted mb-1 text-uppercase fw-bold">Oder manueller Code</div>
                                    <h4 class="font-monospace tracking-wide m-0"><?= htmlspecialchars((string)$pairingData['manual'], ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8') ?></h4>
                                </div>
                            <?php else: ?>
                                <div class="alert alert-warning mt-4">
                                    <i class="fas fa-spinner fa-spin me-2"></i><?= $matterInstalled ? 'Matter Bridge wird gestartet... Bitte in wenigen Sekunden neu laden.' : 'Matter Bridge wartet auf die Dienst-Installation.' ?>
                                </div>
                            <?php endif; ?>
                        <?php endif; ?>

                        <form method="POST" action="index.php?seite=matter" class="mt-4 border-top pt-4" onsubmit="return confirm('Möchten Sie die Matter Bridge wirklich auf den Werkszustand zurücksetzen? Dieser Vorgang löscht alle vorhandenen Kopplungen zu Apple Home, Google Home usw.');">
                            <?= e3dcCsrfInput() ?>
                            <button type="submit" name="reset_pairing" class="btn btn-outline-danger w-100">
                                <i class="fas fa-rotate-left me-2"></i>Auf Kopplung zurücksetzen
                            </button>
                        </form>

                    </div>
                </div>
            </div>

            <div class="col-md-6 mb-4">
                <div class="card shadow-sm h-100">
                    <div class="card-header bg-body-tertiary">
                        <h5 class="mb-0"><i class="fas fa-info-circle me-2"></i>Status & Infrastruktur</h5>
                    </div>
                    <div class="card-body">
                        <ul class="list-group list-group-flush">
                            <li class="list-group-item d-flex justify-content-between align-items-center">
                                Bridge Node-JS Daemon
                                <?php if ($matterDocker && $matterInstalled): ?>
                                    <span class="badge bg-info text-dark rounded-pill">Containerverwaltet</span>
                                <?php elseif ($matterActive): ?>
                                    <span class="badge bg-success rounded-pill">Aktiv (Port 5540)</span>
                                <?php elseif ($matterInstalled): ?>
                                    <span class="badge bg-warning text-dark rounded-pill">Installiert, gestoppt</span>
                                <?php else: ?>
                                    <span class="badge bg-danger rounded-pill">Nicht installiert</span>
                                <?php endif; ?>
                            </li>
                            <li class="list-group-item d-flex justify-content-between align-items-center">
                                Zertifizierung
                                <span class="badge bg-warning text-dark rounded-pill">Uncertified Node</span>
                            </li>
                            <li class="list-group-item d-flex justify-content-between align-items-center">
                                Endpunkte (Geräte)
                                <span class="badge bg-secondary rounded-pill">3 virtuelle Schalter</span>
                            </li>
                        </ul>

                        <div class="alert alert-info mt-4 small">
                            <strong>Betriebsumfang:</strong> Die Bridge stellt drei read-only Statusschalter bereit. Sie übermittelt keine Steuerbefehle an Speicher, Wallbox oder andere Anlagenkomponenten.
                        </div>

                        <div class="alert alert-success mt-3 small">
                            <strong>Aktuelle Matter-Schalter:</strong> E3DC Wallbox aktiv, E3DC PV produziert und E3DC Einspeisung aktiv. Google Home und SmartThings zeigen sie als Steckdosen an, können sie dadurch aber zuverlässig in Routinen verwenden.
                        </div>

                        <div class="alert alert-secondary mt-3 small">
                            <strong><i class="fas fa-network-wired me-1"></i> Mehrere Plattformen (Multi-Admin)</strong><br>
                            Möchten Sie das System mit Apple Home <strong>und</strong> Google Home verwenden?
                            Du musst die Kopplung dafür <strong>nicht</strong> zurücksetzen.
                            Kopple die Bridge zuerst mit System A (z.B. Apple).
                            Öffne dann dort die Bridge-Einstellungen und wähle "Kopplungsmodus aktivieren" (Share Device).
                            Damit erhältst du einen neuen Code, um System B (Google) hinzuzufügen.
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
