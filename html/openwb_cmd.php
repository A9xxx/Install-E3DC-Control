<?php
/**
 * Stillgelegter Kompatibilitätsendpunkt für frühere direkte openWB-Befehle.
 *
 * Die zentrale Wallbox-Policy und der Wallbox-Manager sind der einzige
 * fachliche Entscheider und Hardwareausgang. Dieser Webpfad nimmt deshalb
 * keine Stellwerte mehr an und baut keine Verbindung zu einer Wallbox auf.
 */
require_once __DIR__ . '/helpers.php';

header('Content-Type: application/json; charset=utf-8');
e3dcRequirePostMutation(true);

http_response_code(410);
echo json_encode([
    'success' => false,
    'ok' => false,
    'error' => 'direct_openwb_actuator_retired',
    'message' => 'Direkte openWB-Befehle sind deaktiviert. Die Wallbox wird ausschließlich vom zentralen Wallbox-Manager geführt.',
    'owner' => 'e3dc-wallbox-manager',
    'commands_sent' => 0,
]);
