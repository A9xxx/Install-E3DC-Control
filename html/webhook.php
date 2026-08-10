<?php
// Hardware- und Konfigurationsaktionen aus Push-Nachrichten sind bewusst
// deaktiviert. Ein Service Worker besitzt keinen interaktiven CSRF-Nachweis
// und darf deshalb niemals unmittelbar Anlagenbefehle auslösen.
require_once __DIR__ . '/helpers.php';
sendNoCacheHeaders();
header('Content-Type: application/json; charset=utf-8');
http_response_code(409);
echo json_encode([
    'success' => false,
    'error' => 'Direkte Push-Aktionen sind deaktiviert.',
    'message' => 'Bitte die geschützte Bedienoberfläche öffnen und die gewünschte Aktion dort bestätigen.',
]);
