<?php
/** Experimentelle Kostenrechnung; Bilanz und Quellenverteilung aus der Tagesstatistik. */
require_once __DIR__ . '/helpers.php';

function e3dcCostNumber($value) {
    return !is_bool($value) && is_numeric($value) && is_finite((float)$value) ? (float)$value : null;
}

function e3dcCostEnabled($config) {
    return (string)($config['cost_accounting_quarterhour_enable'] ?? '0') === '1';
}

/** Preisstand zur Messzeit, ohne spätere Konfiguration auf alte Proben anzuwenden. */
function e3dcCostPriceSnapshot($config, $data, $timestamp, $scores = []) {
    $eeg = e3dcBuildEegRevenueConfig($config);
    $dv = e3dcEegConfigBool($config, 'direct_marketing_enable', false);
    $vat = e3dcEegConfigBool($config, 'direct_marketing_input_vat_recoverable', false) ? 1.0
        : 1 + max(0, min(100, e3dcEegConfigFloat($config, 'direct_marketing_service_vat_pct', 19.0))) / 100;
    $market = null;
    $marketSource = 'dv_market_price_missing';
    foreach ($scores as $score) {
        if (!is_array($score)) continue;
        $begin = e3dcCostNumber($score['start_timestamp'] ?? null);
        $end = e3dcCostNumber($score['end_timestamp'] ?? null);
        $at = (float)$timestamp * 1000.0;
        if ($begin !== null && $end !== null && $begin <= $at && $end > $at) {
            $market = e3dcCostNumber($score['direct_marketing_market_price_ct'] ?? null);
            if ($market !== null) $marketSource = (string)($score['direct_marketing_price_source'] ?? 'eco_score');
            break;
        }
    }
    $feed = !empty($eeg['in_support']) ? e3dcCostNumber($eeg['tariff_ct']) : null;
    $reason = $feed === null ? 'feedin_tariff_missing' : '';
    $net = null;
    if ($dv) {
        $reason = 'dv_market_price_missing';
        if ($market !== null) {
            $gross = $market + e3dcEegConfigFloat($config, 'direct_marketing_revenue_offset_ct', 0.0);
            $basis = strtolower(str_replace('-', '_', (string)($config['direct_marketing_variable_fee_basis'] ?? 'sell_revenue')));
            $feeBase = null;
            if (in_array($basis, ['sell_revenue', 'sales_revenue', 'export_revenue'], true)) $feeBase = max(0, $gross);
            if (in_array($basis, ['eeg', 'eeg_compensation', 'market_premium'], true)) $feeBase = $feed;
            if (in_array($basis, ['manual', 'manual_ct', 'manual_ct_per_kwh'], true)) $feeBase = e3dcCostNumber($config['direct_marketing_variable_fee_basis_ct_per_kwh'] ?? null);
            $reason = 'dv_fee_basis_missing';
            if ($feeBase !== null) {
                $fee = (max(0, e3dcEegConfigFloat($config, 'direct_marketing_fee_ct_per_kwh', 0.0))
                    + max(0, $feeBase) * max(0, e3dcEegConfigFloat($config, 'direct_marketing_fee_pct', 0.0)) / 100) * $vat;
                $net = $gross - $fee;
                // Vertragliche Näherung: nach Gebühren mindestens der anzulegende Wert.
                if ($feed !== null) $net = max($net, $feed);
                $reason = '';
            }
        }
        $feed = $net;
    }
    return [
        'billing_price_ct' => e3dcCostNumber($data['price_ct'] ?? null),
        'price_source' => (string)($data['price_source'] ?? 'minute_history'),
        'tariff_type' => (string)($config['stromtarif_typ'] ?? 'unknown'),
        'feedin_price_ct' => $feed, 'feedin_source' => $dv ? 'dv_net_estimate' : 'eeg_config',
        'dv_market_price_ct' => $dv ? $market : null, 'dv_market_source' => $dv ? $marketSource : 'dv_disabled',
        'dv_net_sell_ct' => $net, 'feedin_reason' => $reason, 'dv_estimate' => $dv,
        'monthly_fee_eur' => $dv ? max(0, e3dcEegConfigFloat($config, 'direct_marketing_monthly_fee_eur', 0.0)) * $vat : 0.0,
        'capacity_kwh' => e3dcCostNumber($config['speichergroesse'] ?? null),
    ];
}

/** Alte Proben liefern nur belegte Bezugspreise; heutige Vergütung ist eine Schätzung. */
function e3dcCostHistoricalPrice($row, $config, $timestamp) {
    if (isset($row['cost_prices']) && is_array($row['cost_prices'])) return $row['cost_prices'];
    $price = e3dcCostPriceSnapshot($config, $row, $timestamp);
    $price['price_source'] = 'historical_price_ct';
    $price['tariff_type'] = 'historical_unknown';
    $price['configuration_estimated'] = true;
    $price['feedin_source'] .= '_current_configuration_estimate';
    return $price;
}

/** Zwei Minutenproben auf das überlappende Intervall begrenzen. */
function e3dcCostIntervalEnergy($a, $b, $from, $to, $aTs, $bTs) {
    $rows = [];
    foreach ([$from, $to] as $at) {
        $row = $a;
        foreach ($row as $key => $_) if (strpos($key, 'e_') === 0) unset($row[$key]);
        foreach (['pv', 'pv_total_w', 'pv_external_w', 'grid', 'bat', 'home', 'home_raw', 'wb', 'wb2', 'wp', 'hs', 'climate'] as $key) {
            $left = e3dcCostNumber($a[$key] ?? null);
            $right = e3dcCostNumber($b[$key] ?? null);
            $row[$key] = $left !== null && $right !== null ? $left + ($right - $left) * ($at - $aTs) / ($bTs - $aTs) : null;
        }
        foreach ($b as $key => $value) if (substr($key, -6) === '_valid' && !$value) $row[$key] = false;
        // Nur die Zeitachse des Integrals wird verschoben. So bleiben beide
        // Randproben auch bei Mitternacht im selben Berechnungstag.
        $row['ts'] = gmdate('c', 946728000 + $at - $from);
        $rows[] = json_encode($row, JSON_THROW_ON_ERROR);
    }
    return calculateDailyEnergyStats($rows, ['unrounded_energy' => true]);
}

function e3dcCostProduct($energy, $price) {
    if ($energy === null) return null;
    if (abs($energy) < 1e-12) return 0.0;
    return $price === null ? null : $energy * $price / 100;
}

function e3dcCostAdd(&$sum, $value) {
    $sum = $sum === null || $value === null ? null : $sum + $value;
}

/** Geschätzte Energie wird wie der übrige Inhalt proportional entladen. */
function e3dcCostEstimatedEnergy($state) {
    if (array_key_exists('estimated_kwh', $state)) return e3dcCostNumber($state['estimated_kwh']);
    return !empty($state['estimated']) ? $state['energy_kwh'] : 0.0;
}

/** Start oder Messlücke: Energie verankern, bisherigen Mischpreis erhalten. */
function e3dcCostBatteryAnchor(&$state, $row, $price, $at) {
    $soc = e3dcCostNumber($row['soc'] ?? null);
    $capacity = e3dcCostNumber($price['capacity_kwh'] ?? null);
    $energy = $soc !== null && $soc >= 0 && $soc <= 100 && $capacity !== null && $capacity > 0
        ? $soc * $capacity / 100 : null;
    $sell = e3dcCostNumber($price['feedin_price_ct'] ?? null);
    $oldEnergy = $state['energy_kwh'] ?? null;
    $mean = $state['mean_ct'] ?? null;
    // Ein zeitlich passender gespeicherter Inhalt bleibt auch ohne neue SoC-Probe gültig.
    if ($energy === null && e3dcCostNumber($oldEnergy) !== null && $oldEnergy >= 0
        && empty($state['anchor_pending']) && ($state['last_ts'] ?? null) === $at) $energy = (float)$oldEnergy;
    if ($energy !== null) {
        if ($oldEnergy === null || $mean === null) $mean = $sell;
        elseif ($energy > $oldEnergy + 1e-9) {
            $value = e3dcCostProduct($oldEnergy, $mean);
            e3dcCostAdd($value, e3dcCostProduct($energy - $oldEnergy, $sell));
            $mean = $value !== null ? $value * 100 / $energy : null;
        }
    }
    // Auch ein unveränderter SoC beweist nach einer Lücke nicht die Ladequelle.
    $state = ['energy_kwh' => $energy, 'mean_ct' => $energy !== null && $energy > 1e-9 ? $mean : null,
        'estimated_kwh' => $energy, 'estimated' => $energy === null || $energy > 1e-9,
        'last_ts' => $at, 'anchor_pending' => $energy === null];
}

function e3dcCostBattery(&$state, $in, $out, $chargeValue, $estimatedIn = 0.0) {
    $energy = $state['energy_kwh'];
    $mean = $state['mean_ct'];
    if ($energy === null || $in === null || $out === null) {
        $state['anchor_pending'] = true;
        return null;
    }
    $total = $energy + $in;
    $value = e3dcCostProduct($energy, $mean);
    e3dcCostAdd($value, $chargeValue);
    $mixed = $total > 1e-9 && $value !== null ? $value * 100 / $total : null;
    if ($out > $total + 0.001) {
        $state['anchor_pending'] = true;
        return null;
    }
    $estimated = e3dcCostEstimatedEnergy($state);
    $state['energy_kwh'] = max(0.0, $total - $out);
    if ($state['energy_kwh'] < 1e-9) $state['energy_kwh'] = 0.0;
    $share = $total > 1e-9 && $estimated !== null ? min(1.0, ($estimated + $estimatedIn) / $total) : 0.0;
    $state['estimated_kwh'] = $state['energy_kwh'] * $share;
    $state['estimated'] = $state['estimated_kwh'] > 1e-9;
    $state['mean_ct'] = $state['energy_kwh'] > 1e-9 ? $mixed : null;
    return $mixed;
}

/** Alte Zustände ohne bewertbaren Inhalt werden für neue Zeilen hergeleitet. */
function e3dcCostLegacyStateNeedsReplay($state) {
    return is_array($state) && (($state['energy_kwh'] ?? null) === null
            || (($state['energy_kwh'] ?? 0) > 1e-9 && ($state['mean_ct'] ?? null) === null));
}

/** Nur der Akku-Anschlusswert, keine nachträgliche Festschreibung alter Kosten. */
function e3dcCostBatteryReplay($rows, $before, $config, $state = null) {
    $earliest = strtotime('-30 days', $before);
    $starts = [];
    foreach ($rows as $row) {
        $ts = strtotime($row['ts'] ?? '');
        if ($ts === false || $ts < $earliest - 120 || $ts > $before + 120) continue;
        $slot = intdiv($ts, 900) * 900;
        foreach ([$slot - 900, $slot] as $start) {
            if ($start >= $earliest && $start + 900 <= $before && $ts >= $start - 120 && $ts <= $start + 1020)
                $starts[$start] = true;
        }
    }
    ksort($starts);
    $cursor = 0; $count = count($rows);
    foreach ($starts as $start => $_) {
        if (is_array($state) && isset($state['last_ts']) && $start < $state['last_ts']) continue;
        while ($cursor < $count && strtotime($rows[$cursor]['ts'] ?? '') < $start - 120) $cursor++;
        $samples = [];
        for ($index = $cursor; $index < $count; $index++) {
            $ts = strtotime($rows[$index]['ts'] ?? '');
            if ($ts > min($before + 120, $start + 1020)) break;
            $samples[] = $rows[$index];
        }
        $slot = e3dcCostSlot($samples, $start, $config, $state);
        $state = $slot['state'];
    }
    return $state;
}

/** Web-Vorschau: ausschließlich das angefragte Fenster, zeilenweise lesen. */
function e3dcCostReadPreview($root, $from, $to) {
    $stream = @fopen($root . '/ramdisk/live_history.txt', 'r');
    if ($stream === false) return [];
    $rows = [];
    while (($line = fgets($stream)) !== false) {
        $row = json_decode($line, true);
        if (!is_array($row)) continue;
        $ts = strtotime($row['ts'] ?? '');
        if ($ts !== false && $ts >= $from - 120 && $ts <= $to + 120) $rows[$ts] = $row;
    }
    fclose($stream);
    ksort($rows);
    return array_values($rows);
}

/** Berechnet genau ein UTC-eindeutiges Viertelstundenintervall, ohne zu schreiben. */
function e3dcCostSlot($rows, $start, $config, $state = null) {
    $end = $start + 900;
    $fields = ['pv_kwh', 'grid_in_kwh', 'grid_out_kwh', 'bat_in_kwh', 'bat_out_kwh',
        'home_kwh', 'wb_kwh', 'wb2_kwh', 'wp_kwh', 'climate_kwh', 'pv_to_bat_kwh', 'grid_to_bat_kwh',
        'grid_cost_eur', 'feedin_revenue_eur', 'home_cost_eur', 'dv_fixed_fee_eur'];
    $result = array_fill_keys($fields, 0.0);
    $result += ['ts_start' => $start, 'date' => date('Y-m-d', $start), 'complete' => false,
        'sample_count' => 0, 'coverage_s' => 0, 'reasons' => [], 'prices' => [], 'home_includes' => []];
    $previous = null;
    $priceDurations = [];
    foreach ($rows as $row) {
        if (!is_array($row) || !isset($row['ts'])) continue;
        $ts = strtotime($row['ts']);
        if ($ts === false) continue;
        if ($ts >= $start && $ts < $end) $result['sample_count']++;
        if ($previous !== null) {
            [$aTs, $a] = $previous;
            $from = max($start, $aTs); $to = min($end, $ts);
            if ($from < $to && $ts > $aTs && $ts - $aTs <= 120) {
                $price = e3dcCostHistoricalPrice($a, $config, $from);
                $initial = $state === null;
                if ($initial || ($state['energy_kwh'] ?? null) === null || !empty($state['anchor_pending'])
                    || (isset($state['last_ts']) && $state['last_ts'] !== $from)
                    || ($from === $start && e3dcCostLegacyStateNeedsReplay($state))) {
                    e3dcCostBatteryAnchor($state, $a, $price, $from);
                    $result['reasons'][] = $initial ? 'battery_initial_value_estimated' : 'battery_gap_estimated';
                }
                $estimatedBefore = e3dcCostEstimatedEnergy($state);
                $energy = e3dcCostIntervalEnergy($a, $row, $from, $to, $aTs, $ts);
                $s = $energy['stats'];
                foreach (['pv', 'grid_in', 'grid_out', 'bat_in', 'bat_out', 'home', 'wb', 'wb2', 'wp', 'climate'] as $key)
                    e3dcCostAdd($result[$key . '_kwh'], $s['total_' . $key . '_kwh'] ?? null);
                $pvCharge = $s['pv_bat_kwh'] ?? null;
                $gridCharge = $s['grid_bat_kwh'] ?? null;
                e3dcCostAdd($result['pv_to_bat_kwh'], $pvCharge);
                e3dcCostAdd($result['grid_to_bat_kwh'], $gridCharge);
                $buy = e3dcCostNumber($price['billing_price_ct'] ?? null);
                $sell = e3dcCostNumber($price['feedin_price_ct'] ?? null);
                $chargeValue = e3dcCostProduct($pvCharge, $sell);
                e3dcCostAdd($chargeValue, e3dcCostProduct($gridCharge, $buy));
                $estimatedCharge = !empty($price['configuration_estimated']) || !empty($price['dv_estimate']) ? ($pvCharge ?? 0.0) : 0.0;
                $mixed = e3dcCostBattery($state, $s['total_bat_in_kwh'] ?? null, $s['total_bat_out_kwh'] ?? null, $chargeValue, $estimatedCharge);
                $state['last_ts'] = $to;
                if ($estimatedBefore > 1e-9 && ($s['bat_home_kwh'] ?? 0) > 1e-9)
                    $result['reasons'][] = 'battery_consumption_estimated';
                $homeValue = e3dcCostProduct($s['grid_home_kwh'] ?? null, $buy);
                e3dcCostAdd($homeValue, e3dcCostProduct($s['pv_home_kwh'] ?? null, $sell));
                e3dcCostAdd($homeValue, e3dcCostProduct($s['bat_home_kwh'] ?? null, $mixed));
                e3dcCostAdd($result['home_cost_eur'], $homeValue);
                e3dcCostAdd($result['grid_cost_eur'], e3dcCostProduct($s['total_grid_in_kwh'] ?? null, $buy));
                e3dcCostAdd($result['feedin_revenue_eur'], e3dcCostProduct($s['total_grid_out_kwh'] ?? null, $sell));
                $monthStart = strtotime(date('Y-m-01 00:00:00', $from));
                $monthEnd = strtotime('+1 month', $monthStart);
                e3dcCostAdd($result['dv_fixed_fee_eur'], e3dcCostNumber($price['monthly_fee_eur'] ?? null) === null ? null
                    : $price['monthly_fee_eur'] * ($to - $from) / ($monthEnd - $monthStart));
                $result['home_includes'] = array_values(array_unique(array_merge($result['home_includes'], ($energy['sources']['home_includes_unmeasured_consumers'] ?? []))));
                $key = json_encode($price, JSON_THROW_ON_ERROR);
                if (!isset($priceDurations[$key])) $priceDurations[$key] = ['price' => $price, 'seconds' => 0];
                $priceDurations[$key]['seconds'] += $to - $from;
                $result['coverage_s'] += $to - $from;
                if ($buy === null) $result['reasons'][] = 'billing_price_missing';
                if ($sell === null) $result['reasons'][] = $price['feedin_reason'] ?? 'feedin_price_missing';
                if ($homeValue === null) $result['reasons'][] = 'home_value_unknown';
                if (!empty($price['configuration_estimated'])) $result['reasons'][] = 'historical_configuration_estimated';
                if (!empty($price['dv_estimate'])) $result['reasons'][] = 'dv_revenue_estimate';
            }
        }
        $previous = [$ts, $row];
        if ($ts >= $end) break;
    }
    $result['complete'] = $result['sample_count'] >= 10 && $result['coverage_s'] === 900;
    if (!$result['complete']) {
        $result['reasons'][] = 'minute_history_incomplete';
        foreach ($fields as $field) $result[$field] = null;
        // Die Lücke entwertet den Betrag, aber nicht den bisherigen Mischpreis.
        $anchor = null;
        foreach ($rows as $sample) {
            $at = strtotime($sample['ts'] ?? '');
            if ($at !== false && $at >= $start && $at <= $end) $anchor = [$at, $sample];
        }
        if ($anchor !== null) {
            e3dcCostBatteryAnchor($state, $anchor[1], e3dcCostHistoricalPrice($anchor[1], $config, $anchor[0]), $anchor[0]);
        }
        if ($state !== null) $state['anchor_pending'] = true;
        $result['reasons'][] = 'battery_gap_estimated';
    }
    $state = $state ?? ['energy_kwh' => null, 'mean_ct' => null, 'estimated_kwh' => null, 'estimated' => true];
    $result['battery_energy_kwh'] = $state['energy_kwh'];
    $result['battery_mean_ct'] = $state['mean_ct'];
    $estimated = e3dcCostEstimatedEnergy($state);
    $result['battery_estimated_share'] = $state['energy_kwh'] !== null && $estimated !== null
        ? ($state['energy_kwh'] > 1e-9 ? min(1.0, $estimated / $state['energy_kwh']) : 0.0) : null;
    $result['battery_estimated'] = $result['battery_estimated_share'] === null || $estimated > 1e-9;
    $state['estimated'] = $result['battery_estimated'];
    if ($result['battery_estimated'] && $state['energy_kwh'] > 1e-9) $result['reasons'][] = 'battery_value_estimated';
    if ($state['energy_kwh'] === null || ($state['energy_kwh'] > 1e-9 && $state['mean_ct'] === null)) $result['reasons'][] = 'battery_value_unknown';
    $result['state'] = $state;
    $result['prices'] = array_values($priceDurations);
    foreach (['pv', 'grid_in', 'grid_out', 'bat_in', 'bat_out', 'home', 'wb', 'wb2', 'wp', 'climate'] as $key) {
        if ($result[$key . '_kwh'] === null) $result['reasons'][] = 'measurement_missing_' . $key;
    }
    $result['energy_valid'] = $result['complete'] && $result['home_kwh'] !== null;
    $result['reasons'] = array_values(array_unique($result['reasons']));
    $result['net_cost_eur'] = $result['grid_cost_eur'];
    e3dcCostAdd($result['net_cost_eur'], $result['feedin_revenue_eur'] === null ? null : -$result['feedin_revenue_eur']);
    e3dcCostAdd($result['net_cost_eur'], $result['dv_fixed_fee_eur']);
    foreach (['billing_price_ct', 'feedin_price_ct', 'dv_market_price_ct', 'dv_net_sell_ct'] as $field) {
        $value = 0.0;
        foreach ($result['prices'] as $entry) e3dcCostAdd($value,
            e3dcCostNumber($entry['price'][$field] ?? null) === null ? null : $entry['price'][$field] * $entry['seconds']);
        $result[$field] = $result['coverage_s'] > 0 && $value !== null ? $value / $result['coverage_s'] : null;
    }
    if ($state['energy_kwh'] > 1e-9 && $state['mean_ct'] === null && $result['feedin_price_ct'] === null)
        $result['reasons'][] = 'battery_anchor_tariff_missing';
    return $result;
}

/** Laufende Summen ohne Viertelstunden-Payloads oder Preislisten im Speicher. */
function e3dcCostMergeAggregate(&$sum, $slot) {
    $n = $slot['quarters'] ?? 1;
    $amounts = [];
    foreach (['grid_cost_eur', 'feedin_revenue_eur', 'net_cost_eur', 'home_cost_eur', 'dv_fixed_fee_eur'] as $field) {
        $value = e3dcCostNumber($slot[$field] ?? null);
        $part = $slot['amounts'][$field] ?? ['sum_eur' => $value,
            'valid_quarters' => $value !== null ? $n : 0, 'missing_quarters' => $value !== null ? 0 : $n];
        $previous = $sum['amounts'][$field] ?? null;
        if ($previous === null) $amounts[$field] = $part;
        else {
            $count = $previous['valid_quarters'] + $part['valid_quarters'];
            $amounts[$field] = ['sum_eur' => $count ? ($previous['sum_eur'] ?? 0) + ($part['sum_eur'] ?? 0) : null,
                'valid_quarters' => $count, 'missing_quarters' => $previous['missing_quarters'] + $part['missing_quarters']];
        }
    }
    $n = $slot['quarters'] ?? 1;
    $first = $slot['first_ts'] ?? $slot['ts_start'];
    $last = $slot['last_ts'] ?? $slot['ts_start'];
    if ($sum === null) {
        $sum = $slot;
        unset($sum['prices']);
        $sum['quarters'] = $n; $sum['first_ts'] = $first; $sum['last_ts'] = $last;
        $sum['sealed'] = $slot['sealed'] ?? true;
        $sum['amounts'] = $amounts; $sum['amount_schema'] = 2;
        return;
    }
    foreach ($slot as $field => $value) {
        if (substr($field, -4) === '_eur' || (substr($field, -4) === '_kwh' && $field !== 'battery_energy_kwh'))
            e3dcCostAdd($sum[$field], $value);
    }
    $sum['amounts'] = $amounts; $sum['amount_schema'] = 2;
    $previousLast = $sum['last_ts'];
    $sum['quarters'] += $n;
    $sum['first_ts'] = min($sum['first_ts'], $first); $sum['last_ts'] = max($sum['last_ts'], $last);
    $sum['complete'] = !empty($sum['complete']) && !empty($slot['complete']);
    $sum['energy_valid'] = !empty($sum['energy_valid']) && !empty($slot['energy_valid']);
    $sum['sealed'] = !empty($sum['sealed']) && ($slot['sealed'] ?? true);
    $sum['reasons'] = array_values(array_unique(array_merge($sum['reasons'] ?? [], $slot['reasons'] ?? [])));
    $sum['home_includes'] = array_values(array_unique(array_merge($sum['home_includes'] ?? [], $slot['home_includes'] ?? [])));
    $sum['battery_estimated'] = !empty($sum['battery_estimated']) || !empty($slot['battery_estimated']);
    if ($last >= $previousLast) {
        foreach (['state', 'battery_energy_kwh', 'battery_mean_ct', 'battery_estimated_share'] as $field) $sum[$field] = $slot[$field] ?? null;
    }
    foreach (['billing_price_ct', 'feedin_price_ct', 'dv_market_price_ct', 'dv_net_sell_ct'] as $field) $sum[$field] = null;
}

function e3dcCostFinishAggregate(&$sum, $start, $end) {
    if ($sum === null) {
        $sum = ['ts_start' => $start, 'date' => date('Y-m-d', $start), 'no_data' => true,
            'sealed' => true, 'complete' => false, 'reasons' => ['cost_history_not_recorded']];
        return;
    }
    $sum['ts_start'] = $start; $sum['date'] = date('Y-m-d', $start);
    $sum['recording_gap'] = $sum['quarters'] < max(0, (min($end, intdiv(time(), 900) * 900) - $start) / 900);
    $absent = max(0, (int)((min($end, intdiv(time(), 900) * 900) - $start) / 900) - $sum['quarters']);
    foreach ($sum['amounts'] ?? [] as $field => $part) $sum['amounts'][$field]['missing_quarters'] += $absent;
    if ($sum['quarters'] < ($end - $start) / 900) {
        $sum['complete'] = false;
        $sum['reasons'][] = 'aggregation_incomplete';
        $sum['reasons'] = array_values(array_unique($sum['reasons']));
        $sum['partial_values'] = true;
    }
}

/** Nur allgemeinverständliche Statusdaten, keine Backup- oder Installationspfade. */
function e3dcCostBackfillPublicStatus($state) {
    if (!is_array($state)) return ['status' => 'pending', 'text' => 'Ältere Tage werden ab dem nächsten Archivlauf im Hintergrund geprüft.'];
    $status = $state['status'] ?? 'pending';
    if ($status === 'completed') $text = 'Nachberechnung abgeschlossen am ' . date('d.m.Y H:i', $state['completed_at'])
        . ' (' . $state['added_days'] . ' Tage ergänzt, ' . $state['missing_days'] . ' ohne Minutenwerte)';
    elseif ($status === 'failed') $text = 'Nachberechnung nicht möglich: ' . ($state['reason'] ?? 'Berechnung nach drei fehlgeschlagenen Versuchen beendet.');
    else $text = 'Ältere Tage werden im Hintergrund nachberechnet: ' . ($state['processed'] ?? 0) . ' von ' . ($state['total'] ?? 30) . ' Tagen';
    if (!empty($state['last_error']) && $status !== 'failed') $text .= '. Letzter Fehler: ' . ($state['reason'] ?? 'Die Berechnung wurde vorübergehend unterbrochen.');
    if (!empty($state['battery_transition_gap'])) $text .= '. Am Anschluss an bestehende Kostenwerte weicht der Akkuwert ab; bestehende Zeilen bleiben unverändert.';
    return ['status' => $status, 'processed' => $state['processed'] ?? 0, 'total' => $state['total'] ?? 30, 'text' => $text];
}
