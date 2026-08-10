<?php
header('Cache-Control: no-store, no-cache, must-revalidate, max-age=0');
header('Pragma: no-cache');
header('Expires: 0');

$v4ConfigPath = '/var/www/html/data/e3dc_v4.json';
if (!is_link($v4ConfigPath) && is_file($v4ConfigPath)) {
    header('Location: index.php');
    exit;
}

// Der unterstützte Installer legt die Grundkonfiguration vor dem Webportal an.
// Fehlt sie trotzdem, ist der Installationszustand unvollständig. Ein anonymer
// Browser-Wizard dürfte ohne separat erzeugten Bootstrap-Nachweis ansonsten
// Zugangsdaten schreiben und Dienste starten. Deshalb bleibt dieser Weg
// absichtlich rein informativ und fail-closed.
http_response_code(503);
?>
<!DOCTYPE html>
<html lang="de" data-bs-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>E3DC-Control – Ersteinrichtung erforderlich</title>
    <link href="assets/vendor/bootstrap/css/bootstrap.min.css" rel="stylesheet">
    <link href="assets/vendor/fontawesome/css/all.min.css" rel="stylesheet">
    <style>
        body {
            background: #121212;
            color: #e0e0e0;
            min-height: 100vh;
            display: grid;
            place-items: center;
            font-family: "Segoe UI", Tahoma, Verdana, sans-serif;
        }
        .setup-card {
            width: min(640px, calc(100% - 2rem));
            background: #1e1e1e;
            border: 1px solid #444;
            border-radius: 16px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, .45);
        }
    </style>
</head>
<body>
    <main class="setup-card p-4 p-md-5">
        <div class="d-flex align-items-center gap-3 mb-3">
            <i class="fas fa-shield-halved fa-2x text-warning" aria-hidden="true"></i>
            <h1 class="h3 mb-0">Ersteinrichtung über den Installer erforderlich</h1>
        </div>
        <p>
            Die Grundkonfiguration fehlt. Aus Sicherheitsgründen können Zugangsdaten
            und Dienststarts nicht anonym im Browser eingerichtet werden.
        </p>
        <p class="mb-0 text-body-secondary">
            Bitte verwende die administrative Wiederherstellung am Zielsystem.
            Bei einer Bestandsanlage muss zuerst die vorhandene Konfiguration
            aus einem geprüften Backup wiederhergestellt und die gebundene
            Anlagenrolle kontrolliert werden.
        </p>
    </main>
</body>
</html>
