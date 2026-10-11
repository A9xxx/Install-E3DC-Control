<?php
/** Gemeinsame Tagesberechnung für Web und Statistikwartung. */
if (PHP_SAPI !== 'cli') {
    http_response_code(404);
    exit;
}
require_once __DIR__ . '/helpers.php';
try {
    $lines = preg_split('/\r?\n/', trim(stream_get_contents(STDIN)));
    $result = calculateDailyEnergyStats($lines);
    echo json_encode($result, JSON_THROW_ON_ERROR | JSON_PRESERVE_ZERO_FRACTION);
} catch (Throwable $error) {
    fwrite(STDERR, 'Tagesberechnung fehlgeschlagen: ' . $error->getMessage() . "\n");
    exit(1);
}
