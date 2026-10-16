<?php
/** Gemeinsame Tagesberechnung für Web und Statistikwartung. */
if (PHP_SAPI !== 'cli') {
    http_response_code(404);
    exit;
}
require_once __DIR__ . '/helpers.php';
try {
    $lines = preg_split('/\r?\n/', trim(stream_get_contents(STDIN)));
    $args = getopt('', ['now:', 'climate-day:']);
    $options = [];
    if (isset($args['now'])) {
        if (!is_numeric($args['now']) || !is_finite((float)$args['now'])) throw new InvalidArgumentException('Ungültige Berechnungszeit');
        $options['now'] = (float)$args['now'];
    }
    if (isset($args['climate-day'])) {
        $day = (string)$args['climate-day'];
        if (!preg_match('/^\d{4}-\d{2}-\d{2}$/D', $day) || date('Y-m-d', strtotime($day)) !== $day) {
            throw new InvalidArgumentException('Ungültiger Klima-Tag');
        }
        $result = e3dcClimateDailyBreakdown($day, $options);
    } else {
        $result = calculateDailyEnergyStats($lines, $options);
    }
    echo json_encode($result, JSON_THROW_ON_ERROR | JSON_PRESERVE_ZERO_FRACTION);
} catch (Throwable $error) {
    fwrite(STDERR, 'Tagesberechnung fehlgeschlagen: ' . $error->getMessage() . "\n");
    exit(1);
}
