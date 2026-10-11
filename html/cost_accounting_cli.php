<?php
if (PHP_SAPI !== 'cli') { http_response_code(404); exit; }
require_once __DIR__ . '/cost_accounting.php';
try {
    $input = json_decode(stream_get_contents(STDIN), true, 512, JSON_THROW_ON_ERROR);
    $state = $input['state'] ?? null;
    if (($input['action'] ?? '') === 'replay') {
        echo json_encode(e3dcCostBatteryReplay($input['rows'], $input['before'], $input['config'], $state), JSON_THROW_ON_ERROR);
        exit;
    }
    $rows = $input['rows'];
    $slots = [];
    $stamps = array_map(function($row) { return strtotime($row['ts'] ?? ''); }, $rows);
    $cursor = 0; $count = count($rows); $replayAttempted = false;
    foreach ($input['starts'] as $start) {
        $existing = $input['existing'][(string)$start] ?? null;
        if (is_array($existing)) { $state = $existing['state']; continue; }
        if (!$replayAttempted && ($state === null || e3dcCostLegacyStateNeedsReplay($state))) {
            $state = ($input['replay_state'] ?? null) ?: (e3dcCostBatteryReplay($input['bootstrap_rows'] ?? $rows, $start, $input['config']) ?? $state);
            $replayAttempted = true;
        }
        while ($cursor < $count && $stamps[$cursor] < $start - 120) $cursor++;
        $slotRows = [];
        for ($index = $cursor; $index < $count && $stamps[$index] <= $start + 1020; $index++) $slotRows[] = $rows[$index];
        $slot = e3dcCostSlot($slotRows, $start, $input['config'], $state);
        $slots[] = $slot; $state = $slot['state'];
    }
    echo json_encode($slots, JSON_THROW_ON_ERROR | JSON_PRESERVE_ZERO_FRACTION);
} catch (Throwable $error) {
    fwrite(STDERR, 'Kostenberechnung fehlgeschlagen: ' . $error->getMessage() . "\n");
    exit(1);
}
