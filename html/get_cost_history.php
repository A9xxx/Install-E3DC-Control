<?php
require_once __DIR__ . '/cost_accounting.php';
requireWebAuth(true);
header('Content-Type: application/json; charset=utf-8');
sendNoCacheHeaders();
$quarterStreaming = false;
try {
    $root = '/var/www/html';
    $config = json_decode(@file_get_contents($root . '/data/e3dc_v4.json'), true) ?: [];
    if (!e3dcCostEnabled($config)) {
        echo json_encode(['enabled' => false, 'rows' => []]); exit;
    }
    $period = (string)($_GET['period'] ?? '');
    $day = (string)($_GET['date'] ?? date('Y-m-d'));
    if ($period === 'month' && preg_match('/^\d{4}-\d{2}$/D', $day)) $day .= '-01';
    if ($period === 'year' && preg_match('/^\d{4}$/D', $day)) $day .= '-01-01';
    $days = (int)($_GET['days'] ?? 1);
    $resolution = (string)($_GET['resolution'] ?? ($period === '' ? 'quarter' : ($period === 'year' ? 'month' : ($period === 'day' ? 'hour' : 'day'))));
    if (!preg_match('/^\d{4}-\d{2}-\d{2}$/D', $day) || $days < 1 || $days > 31
        || !in_array($period, ['', 'day', 'week', 'month', 'year'], true)
        || !in_array($resolution, ['quarter', 'hour', 'day', 'month'], true)) throw new InvalidArgumentException('Ungültiger Zeitraum');
    $start = strtotime($day . ' 00:00:00');
    if ($start === false || date('Y-m-d', $start) !== $day) throw new InvalidArgumentException('Ungültiges Datum');
    if ($period === 'week') $start = strtotime('-' . ((int)date('N', $start) - 1) . ' days', $start);
    if ($period === 'month') $start = strtotime(date('Y-m-01 00:00:00', $start));
    if ($period === 'year') $start = strtotime(date('Y-01-01 00:00:00', $start));
    $end = strtotime($period === 'week' ? '+7 days' : ($period === 'month' ? '+1 month'
        : ($period === 'year' ? '+1 year' : '+' . ($period === 'day' ? 1 : $days) . ' days')), $start);
    // Details werden tageweise nachgeladen; auch fremde Aufrufer bleiben speicherbegrenzt.
    if (in_array($resolution, ['quarter', 'hour'], true) && $end - $start > 32 * 86400)
        throw new InvalidArgumentException('Stunden und Viertelstunden tageweise auswählen');
    $now = time(); $todayEnd = strtotime('tomorrow');
    $readEnd = min($end, $todayEnd);
    $db = null; $total = null; $backfill = null; $hasCosts = false; $cacheValid = false;
    $preview = []; $state = null; $sealedPreview = [];
    $shape = ['first' => null, 'last' => null, 'n' => null];
    $previewStart = intdiv($now, 3600) * 3600 - 3600;
    $previewEnd = intdiv($now, 900) * 900;
    if (is_file($root . '/data/e3dc_stats.db')) {
        $openFlags = defined('Pdo\\Sqlite::ATTR_OPEN_FLAGS')
            ? constant('Pdo\\Sqlite::ATTR_OPEN_FLAGS') : constant('PDO::SQLITE_ATTR_OPEN_FLAGS');
        $db = new PDO('sqlite:' . $root . '/data/e3dc_stats.db', null, null,
            [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION, $openFlags => 1]);
        $db->exec('PRAGMA query_only=ON');
        $db->beginTransaction(); // Alle Summen und die Vorschau beziehen sich auf denselben Bestand.
        $tables = $db->query("SELECT name FROM sqlite_master WHERE type='table'")->fetchAll(PDO::FETCH_COLUMN);
        $hasCosts = in_array('quarter_hour_costs', $tables, true);
        if (in_array('cost_backfill_state', $tables, true)) {
            $payload = $db->query('SELECT payload FROM cost_backfill_state WHERE id=1')->fetchColumn();
            if ($payload !== false) $backfill = json_decode($payload, true, 512, JSON_THROW_ON_ERROR);
        }
        if ($hasCosts) {
            $shape['first'] = $db->query('SELECT MIN(ts_start) FROM quarter_hour_costs')->fetchColumn();
            $shape['last'] = $db->query('SELECT MAX(ts_start) FROM quarter_hour_costs')->fetchColumn();
            if (in_array('cost_row_count', $tables, true)) {
                $count = $db->query('SELECT n FROM cost_row_count WHERE id=1')->fetchColumn();
                if ($count !== false) $shape['n'] = (int)$count;
            }
            if (in_array('cost_aggregates', $tables, true)) {
                $payload = $db->query("SELECT payload FROM cost_aggregates WHERE key='total'")->fetchColumn();
                if ($payload !== false) {
                    $total = json_decode($payload, true, 512, JSON_THROW_ON_ERROR);
                    $cacheValid = ($total['amount_schema'] ?? null) === 2 && $shape['n'] !== null && (int)$shape['n'] === ($total['quarters'] ?? -1)
                        && (int)$shape['first'] === ($total['first_ts'] ?? -1) && (int)$shape['last'] === ($total['last_ts'] ?? -1);
                }
            }
            $query = $db->prepare('SELECT ts_start,payload FROM quarter_hour_costs WHERE ts_start<? ORDER BY ts_start DESC LIMIT 1');
            $query->execute([$previewStart]); $previous = $query->fetch(PDO::FETCH_ASSOC);
            if ($previous !== false) {
                $state = json_decode($previous['payload'], true, 512, JSON_THROW_ON_ERROR)['state'];
                $state['last_ts'] = (int)$previous['ts_start'] + 900;
            }
            $query = $db->prepare('SELECT ts_start,payload FROM quarter_hour_costs WHERE ts_start>=? AND ts_start<? ORDER BY ts_start');
            $query->execute([$previewStart, $previewEnd]);
            while ($row = $query->fetch(PDO::FETCH_ASSOC)) $sealedPreview[(int)$row['ts_start']] = json_decode($row['payload'], true, 512, JSON_THROW_ON_ERROR);
        }
    }
    if ($backfill === null) $backfill = json_decode(@file_get_contents($root . '/data/cost_backfill_failure.json'), true);
    $history = e3dcCostReadPreview($root, $previewStart, $previewEnd);
    for ($ts = $previewStart; $ts < $previewEnd; $ts += 900) {
        if (isset($sealedPreview[$ts])) { $state = $sealedPreview[$ts]['state']; continue; }
        // Ohne Minutenwerte entsteht auch in der Vorschau keine erfundene Kostenzeile.
        if (!$history) continue;
        $pending = $state === null || e3dcCostLegacyStateNeedsReplay($state)
            || !empty($state['anchor_pending']) || ($state['last_ts'] ?? null) !== $ts;
        $slot = e3dcCostSlot($history, $ts, $config, $state);
        if ($pending) {
            $slot['battery_mean_ct'] = null; $slot['home_cost_eur'] = null; $slot['state']['mean_ct'] = null;
            $slot['reasons'][] = 'battery_archive_anchor_pending';
        }
        $slot['sealed'] = false; $preview[$ts] = $slot; $state = $slot['state'];
    }
    if (!$cacheValid) {
        $total = null;
        if ($hasCosts) {
            // Vor dem ersten Archivlauf keine Gesamthistorie im Web neu dekodieren.
            if ($shape['first'] !== null) $total = ['ts_start' => (int)$shape['first'],
                'first_ts' => (int)$shape['first'], 'last_ts' => (int)$shape['last'], 'quarters' => $shape['n'] ?? 0,
                'complete' => false, 'sealed' => true, 'no_data' => true, 'reasons' => ['cost_total_archive_pending']];
        }
    }
    foreach ($preview as $slot) e3dcCostMergeAggregate($total, $slot);
    $finishTotal = function() use (&$total) {
        if ($total === null) return;
        $first = $total['first_ts']; $last = $total['last_ts'];
        e3dcCostFinishAggregate($total, strtotime(date('Y-m-d 00:00:00', $first)), strtotime(date('Y-m-d', $last) . ' +1 day'));
        $total['label'] = 'Gesamt seit ' . date('d.m.Y', $first); $total['date'] = date('Y-m-d', $first);
    };
    if ($resolution === 'quarter') {
        // Auch die alte Monatsabfrage gibt ihre Viertelstunden einzeln aus.
        // Preisdetails bleiben erhalten, ohne alle Zeilen im PHP-Speicher zu halten.
        $summary = null; $firstRow = true; $pendingPreview = [];
        foreach ($preview as $ts => $slot) if ($ts >= $start && $ts < $readEnd) $pendingPreview[$ts] = $slot;
        $emitQuarter = function($slot) use (&$summary, &$firstRow) {
            $quarter = null;
            e3dcCostMergeAggregate($quarter, $slot);
            e3dcCostMergeAggregate($summary, $slot);
            e3dcCostFinishAggregate($quarter, $slot['ts_start'], $slot['ts_start'] + 900);
            if (isset($slot['prices'])) $quarter['prices'] = $slot['prices'];
            $quarter['label'] = date('d.m. H:i P', $slot['ts_start']);
            $encoded = json_encode($quarter, JSON_THROW_ON_ERROR | JSON_PRESERVE_ZERO_FRACTION);
            echo ($firstRow ? '' : ',') . $encoded;
            $firstRow = false;
        };
        $quarterStreaming = true;
        echo '{"rows":[';
        if ($hasCosts && $start < $readEnd) {
            $query = $db->prepare('SELECT ts_start,payload FROM quarter_hour_costs WHERE ts_start>=? AND ts_start<? ORDER BY ts_start');
            $query->execute([$start, $readEnd]);
            while ($row = $query->fetch(PDO::FETCH_ASSOC)) {
                while ($pendingPreview && array_key_first($pendingPreview) < (int)$row['ts_start']) {
                    $ts = array_key_first($pendingPreview); $emitQuarter($pendingPreview[$ts]); unset($pendingPreview[$ts]);
                }
                $slot = json_decode($row['payload'], true, 512, JSON_THROW_ON_ERROR); $slot['sealed'] = true;
                $emitQuarter($slot);
            }
        }
        foreach ($pendingPreview as $slot) $emitQuarter($slot);
        e3dcCostFinishAggregate($summary, $start, $end); $finishTotal();
        if ($db !== null) $db->rollBack();
        $metadata = ['enabled' => true, 'experimental' => true, 'period' => $period ?: 'legacy', 'resolution' => $resolution,
            'start' => date('Y-m-d', $start), 'end' => date('Y-m-d', $end), 'summary' => $summary,
            'total' => $total, 'backfill' => e3dcCostBackfillPublicStatus($backfill)];
        echo '],' . substr(json_encode($metadata, JSON_THROW_ON_ERROR | JSON_PRESERVE_ZERO_FRACTION), 1);
        exit;
    }
    $groups = []; $summary = null;
    $keyOf = function($ts) use ($resolution) {
        return $resolution === 'month' ? strtotime(date('Y-m-01 00:00:00', $ts))
            : ($resolution === 'day' ? strtotime(date('Y-m-d 00:00:00', $ts))
                : ($resolution === 'hour' ? intdiv($ts, 3600) * 3600 : $ts));
    };
    $consume = function($slot) use (&$groups, &$summary, $keyOf, $resolution) {
        $key = $keyOf($slot['ts_start']);
        if (!isset($groups[$key])) $groups[$key] = null;
        e3dcCostMergeAggregate($groups[$key], $slot);
        e3dcCostMergeAggregate($summary, $slot);
        if ($resolution === 'quarter' && isset($slot['prices'])) $groups[$key]['prices'] = $slot['prices'];
    };
    if ($hasCosts && $start < $readEnd) {
        if ($cacheValid && in_array($resolution, ['day', 'month'], true)) {
            $query = $db->prepare("SELECT payload FROM cost_aggregates WHERE key>=? AND key<? ORDER BY key");
            $query->execute(['day:' . date('Y-m-d', $start), 'day:' . date('Y-m-d', $readEnd)]);
            while ($row = $query->fetch(PDO::FETCH_ASSOC)) $consume(json_decode($row['payload'], true, 512, JSON_THROW_ON_ERROR));
        } else {
            $query = $db->prepare('SELECT payload FROM quarter_hour_costs WHERE ts_start>=? AND ts_start<? ORDER BY ts_start');
            $query->execute([$start, $readEnd]);
            while ($row = $query->fetch(PDO::FETCH_ASSOC)) {
                $slot = json_decode($row['payload'], true, 512, JSON_THROW_ON_ERROR); $slot['sealed'] = true;
                $consume($slot);
            }
        }
    }
    foreach ($preview as $ts => $slot) {
        if ($ts >= $start && $ts < $readEnd) $consume($slot);
    }
    foreach ($groups as $key => &$group) {
        $until = $resolution === 'month' ? strtotime('+1 month', $key) : ($resolution === 'day' ? strtotime('+1 day', $key)
            : $key + ($resolution === 'hour' ? 3600 : 900));
        e3dcCostFinishAggregate($group, $key, $until);
        $group['label'] = date($resolution === 'month' ? 'm.Y' : ($resolution === 'day' ? 'd.m.Y' : 'd.m. H:i P'), $key);
    }
    unset($group);
    if (in_array($resolution, ['day', 'month'], true)) {
        for ($at = $start; $at < $readEnd; $at = strtotime($resolution === 'month' ? '+1 month' : '+1 day', $at)) {
            if (!isset($groups[$at])) $groups[$at] = ['ts_start' => $at, 'date' => date('Y-m-d', $at),
                'label' => date($resolution === 'month' ? 'm.Y' : 'd.m.Y', $at), 'no_data' => true,
                'sealed' => true, 'complete' => false, 'reasons' => ['cost_history_not_recorded']];
        }
    }
    ksort($groups);
    e3dcCostFinishAggregate($summary, $start, $end);
    $finishTotal();
    if ($db !== null) $db->rollBack();
    echo json_encode(['enabled' => true, 'experimental' => true, 'period' => $period ?: 'legacy', 'resolution' => $resolution,
        'start' => date('Y-m-d', $start), 'end' => date('Y-m-d', $end), 'summary' => $summary,
        'total' => $total, 'backfill' => e3dcCostBackfillPublicStatus($backfill), 'rows' => array_values($groups)], JSON_THROW_ON_ERROR | JSON_PRESERVE_ZERO_FRACTION);
} catch (InvalidArgumentException $error) {
    http_response_code(400); echo json_encode(['error' => $error->getMessage()]);
} catch (Throwable $error) {
    http_response_code(503);
    if ($quarterStreaming) echo '],"error":' . json_encode('Kostenhistorie derzeit nicht lesbar') . '}';
    else echo json_encode(['error' => 'Kostenhistorie derzeit nicht lesbar']);
    error_log('Kostenhistorie: ' . $error->getMessage());
}
