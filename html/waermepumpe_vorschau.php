<?php
/**
 * waermepumpe_vorschau.php – neue Ansicht der Wärmepumpe mit Anlagenbild (Vorschau, experimentell).
 *
 * Nur Anzeige. Die Seite liest dieselben Dateien wie html/waermepumpe.php (Ramdisk-JSON je
 * WP-Typ, Konfiguration, Tagesarchiv für die Tages-AZ) und zusätzlich die Wetterlage der
 * aktuellen Stunde aus weather_alerts.json. Sie startet keine Prozesse und schreibt keine Dateien.
 * Die Knöpfe Boost, 1× Warmwasser und Automatik senden an die vorhandenen Handler der bisherigen
 * Seite (seite=waermepumpe in der aufrufenden Oberfläche index.php oder mobile.php, gleiche Felder, CSRF-Token).
 *
 * Fehlende oder ungültige Werte bleiben null und erscheinen als „--“ mit dem Grund im Tooltip.
 * Aufbau: wpv_collect_inputs() liest, wpv_build_state() rechnet, wpv_render() stellt dar.
 * Die Aktualisierung lädt dieselbe Seite lesend nach (<Oberfläche>?seite=waermepumpe_vorschau&ajax=1).
 */

if (!function_exists('wpv_render')) {

function wpv_h($value): string
{
    return htmlspecialchars((string)$value, ENT_QUOTES, 'UTF-8');
}

/** Endliche Zahl oder null; Wahrheitswerte, leere Texte und Platzhalter wie „---“ sind kein Messwert. */
function wpv_num($value): ?float
{
    if (is_bool($value) || $value === null || $value === '') return null;
    if (is_string($value)) $value = str_replace(',', '.', trim($value));
    if (!is_numeric($value)) return null;
    $number = (float)$value;
    return is_finite($number) ? $number : null;
}

/** Erster numerischer Wert aus einer Liste von Feldnamen. */
function wpv_pick(array $data, array $keys): ?float
{
    foreach ($keys as $key) {
        if (!array_key_exists($key, $data)) continue;
        $value = wpv_num($data[$key]);
        if ($value !== null) return $value;
    }
    return null;
}

function wpv_fmt(?float $value, int $decimals = 1, string $unit = ''): string
{
    if ($value === null) return '--';
    return number_format($value, $decimals, ',', '.') . $unit;
}

function wpv_read_json(string $path): ?array
{
    if ($path === '' || !is_file($path) || !is_readable($path)) return null;
    $raw = @file_get_contents($path);
    if (!is_string($raw) || $raw === '') return null;
    $decoded = json_decode($raw, true);
    return is_array($decoded) ? $decoded : null;
}

/**
 * Chip „… wartet“ aus der Zurückhaltung des Kanalautomaten (heatpump_channel_dispatch.hold).
 * Rechnet nur die Restzeit bis until_ts; ohne Zurückhaltung oder nach deren Ende null.
 * $endTs: Ende des Befehls; endet die Sperre erst mit oder nach ihm, gibt es keinen Neustart.
 */
function wpv_hold_badge(array $json, array $channels, string $label, int $now, ?float $endTs = null): ?array
{
    $hold = $json['heatpump_channel_dispatch']['hold'] ?? null;
    if (!is_array($hold)) return null;
    $texts = [
        'e3dc_live_gap' => 'Keine gültigen E3DC-Livedaten: kein neuer Start, bis die Daten wieder gültig sind',
        'restart_block' => 'Wiedereinschaltsperre nach Verdichterstillstand',
        'stop_until' => 'Sperrzeit nach einer Rücknahme',
    ];
    foreach ($channels as $channel) {
        $entry = $hold[$channel] ?? null;
        if (!is_array($entry)) continue;
        $reason = trim((string)($entry['reason'] ?? ''));
        if ($reason === '') continue;
        $title = $texts[$reason] ?? ('Zurückhaltung: ' . $reason);
        $until = wpv_num($entry['until_ts'] ?? null);
        if ($until === null) {
            return ['b-warn', $label . ' wartet' . ($reason === 'e3dc_live_gap' ? ': E3DC-Daten fehlen' : ''), $title];
        }
        if ($until <= $now) continue;
        if ($endTs !== null && $endTs > 0 && $until >= $endTs) {
            return ['b-warn', $label . ' wartet: kein Neustart vor Ablauf',
                $title . ' bis ' . date('H:i', (int)$until) . '; der Befehl endet vorher um ' . date('H:i', (int)$endTs)];
        }
        $minutes = (int)ceil(($until - $now) / 60);
        return ['b-warn', $label . ' wartet: Sperre noch ' . $minutes . ' min', $title . ' bis ' . date('H:i', (int)$until)];
    }
    return null;
}

/** Pufferfühler: keiner (Standard), externer Rücklauf der Luxtronik oder Pufferfühler der Stiebel-ISG. */
function wpv_buffer_sensor($raw): string
{
    $value = strtolower(trim((string)$raw));
    if (in_array($value, ['luxtronik_ruecklauf_extern', 'ruecklauf_extern'], true)) return 'luxtronik_ruecklauf_extern';
    if (in_array($value, ['stiebel_puffer', 'puffer_ist'], true)) return 'stiebel_puffer';
    return 'none';
}

/** Die Ansicht erscheint nur mit eingeschaltetem Schalter und aktiver Wärmepumpe. */
function wpv_page_allowed(array $conf): array
{
    if (!cfgBool($conf['wp_page_preview_enable'] ?? false, false)) {
        return ['ok' => false, 'why' => 'Die neue Wärmepumpen-Ansicht ist ausgeschaltet (Konfiguration, Wärmequelle: „Neue Wärmepumpen-Ansicht“).'];
    }
    if ((int)($conf['wp_type'] ?? -1) === 2 || !isHeatpumpEnabledConfig($conf)) {
        return ['ok' => false, 'why' => 'Die neue Ansicht gibt es nur für eine aktive Wärmepumpe.'];
    }
    return ['ok' => true, 'why' => ''];
}

function wpv_hex_mix(string $a, string $b, float $t): string
{
    $t = max(0.0, min(1.0, $t));
    $out = '#';
    for ($i = 0; $i < 3; $i++) {
        $x = hexdec(substr($a, 1 + $i * 2, 2));
        $y = hexdec(substr($b, 1 + $i * 2, 2));
        $out .= str_pad(dechex((int)round($x + ($y - $x) * $t)), 2, '0', STR_PAD_LEFT);
    }
    return $out;
}

/** Farbe einer Wassertemperatur, gleiche Stützstellen wie im Prototyp. */
function wpv_temp_color(float $t): string
{
    $k = [10, 25, 35, 45, 55, 65];
    $c = ['#3b6fe0', '#2fa4d9', '#2bb59a', '#f0a23a', '#e5484d', '#c81e4a'];
    if ($t <= $k[0]) return $c[0];
    for ($i = 1; $i < count($k); $i++) {
        if ($t <= $k[$i]) return wpv_hex_mix($c[$i - 1], $c[$i], ($t - $k[$i - 1]) / ($k[$i] - $k[$i - 1]));
    }
    return $c[count($c) - 1];
}

/** Wetterlage der aktuellen Stunde (WMO-Code) aus weather_alerts.json; ohne Beleg neutral. */
function wpv_weather(?array $alerts, int $now): array
{
    if (!is_array($alerts)) return ['wx' => 'none', 'why' => 'Keine Wetterdaten vorhanden'];
    $fetched = is_string($alerts['fetched_at'] ?? null) ? strtotime((string)$alerts['fetched_at']) : false;
    if ($fetched === false || abs($now - $fetched) > 3 * 3600) {
        return ['wx' => 'none', 'why' => 'Wetterdaten älter als 3 Stunden'];
    }
    $risk = is_array($alerts['risk'] ?? null) ? $alerts['risk'] : [];
    $code = $risk['weather_code'] ?? null;
    $ts = wpv_num($risk['ts'] ?? null);
    if (!is_int($code) && !(is_numeric($code) && (float)$code == (int)$code)) {
        return ['wx' => 'none', 'why' => 'Kein Wettercode für die aktuelle Stunde'];
    }
    if ($ts === null || abs($now - $ts) > 3600) {
        return ['wx' => 'none', 'why' => 'Der gemeldete Wettercode gilt nicht für die aktuelle Stunde'];
    }
    $code = (int)$code;
    if ($code <= 1) $wx = 'klar';
    elseif ($code === 2) $wx = 'leicht';
    elseif (in_array($code, [3, 45, 48], true)) $wx = 'bedeckt';
    elseif (($code >= 71 && $code <= 77) || $code === 85 || $code === 86) $wx = 'schnee';
    elseif (($code >= 51 && $code <= 67) || ($code >= 80 && $code <= 82) || $code >= 95) $wx = 'regen';
    else return ['wx' => 'none', 'why' => 'Unbekannter Wettercode ' . $code];
    return ['wx' => $wx, 'why' => 'Wettercode ' . $code . ' (Open-Meteo, aktuelle Stunde)'];
}

/** Betriebsart aus dem Klartext der Wärmepumpe. */
function wpv_mode_from_text(string $text): ?string
{
    $t = strtolower(trim($text));
    if ($t === '' || $t === '--') return null;
    if (strpos($t, 'abtau') !== false) return 'abtauen';
    if (strpos($t, 'evu') !== false || strpos($t, 'sperre') !== false) return 'evu';
    if (strpos($t, 'warmw') !== false || preg_match('/\bww\b/u', $t)) return 'ww';
    if (strpos($t, 'heiz') !== false) return 'heizen';
    if (strpos($t, 'kühl') !== false || strpos($t, 'kuehl') !== false) return 'kühlen';
    if (strpos($t, 'standby') !== false || strpos($t, 'bereit') !== false || strpos($t, 'keine anf') !== false) return 'bereit';
    return null;
}

/**
 * Liest alle Quellen, nur lesend. Pfade und Uhrzeit sind für den Testrahmen einstellbar.
 * Gleiche Auswahl wie html/waermepumpe.php (Z. 42–149).
 */
function wpv_collect_inputs(array $conf, array $opts = []): array
{
    $rd = rtrim((string)($opts['ramdisk'] ?? '/var/www/html/ramdisk'), '/');
    $dd = rtrim((string)($opts['data'] ?? '/var/www/html/data'), '/');
    $now = (int)($opts['now'] ?? time());
    $wpType = (int)($conf['wp_type'] ?? -1);

    $luxFile = $rd . '/luxtronik.json';
    $stiebelFile = $rd . '/stiebel_isg.json';
    $dimplexFile = $rd . '/dimplex_wpm.json';
    $liveFile = $rd . '/waermepumpe.json';
    $json = null;
    $sourceFile = '';
    $ageS = null;
    $stiebelStatus = 'not_applicable';

    if ($wpType === 4) {
        $selection = e3dcSelectFreshManufacturerPayload([$stiebelFile, $liveFile], 'Stiebel', 150, $now);
        $stiebelStatus = (string)$selection['status'];
        $ageS = is_numeric($selection['age_s'] ?? null) ? (int)$selection['age_s'] : null;
        $sourceFile = (string)($selection['path'] ?? '');
        $json = ($stiebelStatus === 'live')
            ? $selection['payload']
            : ['success' => false, 'error' => (string)$selection['error'], 'data' => [], 'status' => []];
    } elseif ($wpType === 1 && is_file($liveFile)) {
        $json = wpv_read_json($liveFile);
        $sourceFile = $liveFile;
        $em = wpv_read_json($luxFile);
        if (is_array($json) && is_array($em)) {
            foreach (['mb_state', 'boost_active', 'pv_pause_active', 'pv_pause_owner', 'auto_mode'] as $k) {
                if (array_key_exists($k, $em)) $json[$k] = $em[$k];
            }
        }
    } elseif ($wpType === 5) {
        if (is_file($dimplexFile)) {
            $json = wpv_read_json($dimplexFile);
            $sourceFile = $dimplexFile;
        } elseif (is_file($liveFile)) {
            $candidate = wpv_read_json($liveFile);
            $candidateData = is_array($candidate) ? ($candidate['data'] ?? $candidate) : [];
            $sourceText = strtolower((string)(($candidate['source'] ?? '') . ' ' . ($candidateData['Quelle'] ?? '') . ' ' . ($candidateData['Hersteller'] ?? '')));
            if (strpos($sourceText, 'dimplex') !== false) {
                $json = $candidate;
                $sourceFile = $liveFile;
            }
        }
        $em = wpv_read_json($luxFile);
        if (is_array($json) && is_array($em)) {
            foreach (['mb_state', 'boost_active', 'pv_pause_active', 'pv_pause_owner', 'auto_mode'] as $k) {
                if (array_key_exists($k, $em)) $json[$k] = $em[$k];
            }
        }
    } elseif ($wpType === 6) {
        $json = e3dcHeatpumpPmPagePayload($rd . '/live_data_py.json', $now);
        $sourceFile = $rd . '/live_data_py.json';
        $ageS = is_numeric($json['age_s'] ?? null) ? (int)$json['age_s'] : null;
    } elseif ($wpType === 0 && is_file($luxFile)) {
        $json = wpv_read_json($luxFile);
        $sourceFile = $luxFile;
    } elseif (is_file($liveFile)) {
        $json = wpv_read_json($liveFile);
        $sourceFile = $liveFile;
    }
    if (!is_array($json)) {
        $json = ['success' => false, 'error' => 'Warte auf Daten vom Hintergrunddienst', 'data' => [], 'status' => []];
    }
    $dataMtime = ($sourceFile !== '') ? @filemtime($sourceFile) : false;
    if ($ageS === null && $dataMtime !== false) $ageS = max(0, $now - (int)$dataMtime);

    // WP-Leistung ohne Herstellerfeld: letzter Satz des Live-Verlaufs (waermepumpe.php Z. 338–346)
    $historyWpW = null;
    $liveHistory = $rd . '/live_history.txt';
    if ($wpType !== 6 && is_file($liveHistory) && is_readable($liveHistory)) {
        $lines = @file($liveHistory, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
        if (is_array($lines) && $lines) {
            $last = json_decode((string)array_pop($lines), true);
            if (is_array($last)) $historyWpW = wpv_num($last['wp'] ?? null);
        }
    }

    // Tagesbeginn der Energiezähler für die Tages-AZ (waermepumpe.php Z. 382–390, 643–647)
    $emMtime = @filemtime($luxFile);
    $emJson = ($wpType === 0) ? $json : (($emMtime !== false) ? wpv_read_json($luxFile) : null);
    $today = e3dcHeatpumpDataDay(is_array($emJson) ? ($emJson['ts'] ?? '') : '', $now, ($emMtime !== false) ? (int)$emMtime : null);
    $data = $json['data'] ?? $json;
    $counters = e3dcHeatpumpEnergyCounters(is_array($data) ? $data : []);
    $dayStart = ['heat_kwh' => null, 'elec_kwh' => null, 'ts' => null, 'source' => null];
    if (!in_array($wpType, [1, 4, 6], true) && $counters['heat_kwh'] !== null && $counters['elec_kwh'] !== null) {
        $dayStart = e3dcHeatpumpDayCounterStart(
            $dd . '/luxtronik_archive/luxtronik_' . $today . '.json',
            $rd . '/luxtronik_history.json',
            $today
        );
    }

    return [
        'now' => $now,
        'wp_type' => $wpType,
        'json' => $json,
        'data_age_s' => $ageS,
        'data_mtime' => ($dataMtime !== false) ? (int)$dataMtime : null,
        'stiebel_status' => $stiebelStatus,
        'history_wp_w' => $historyWpW,
        'day_start' => $dayStart,
        'flag_manual_boost' => is_file($rd . '/manual_boost.flag') || is_file($dd . '/morning_boost_state.json'),
        'flag_manual_ww' => is_file($rd . '/manual_ww_boost.flag'),
        'weather_alerts' => wpv_read_json($rd . '/weather_alerts.json'),
    ];
}

/** Rechnet aus Konfiguration und gelesenen Daten den Anzeigezustand. Liest und schreibt nichts. */
function wpv_build_state(array $conf, array $in): array
{
    $now = (int)($in['now'] ?? time());
    $wpType = (int)($conf['wp_type'] ?? -1);
    $json = is_array($in['json'] ?? null) ? $in['json'] : [];
    $rawData = $json['data'] ?? $json;
    $rawData = is_array($rawData) ? $rawData : [];
    $success = !empty($json['success']);
    $error = trim((string)($json['error'] ?? ''));
    $noData = 'Keine gültigen Live-Daten der Wärmepumpe' . ($error !== '' ? ': ' . $error : '');
    // Ohne gültigen Datensatz werden keine Einzelwerte angezeigt, auch keine alten.
    $d = $success ? $rawData : [];
    $why = static function (string $reason) use ($success, $noData): string {
        return $success ? $reason : $noData;
    };
    $val = static function (?float $value, string $reason): array {
        return ['v' => $value, 'why' => $value === null ? $reason : ''];
    };

    $source = e3dcNormalizeHeatSourceType($conf['wp_source_type'] ?? 'auto');
    $sceneSource = ['sole' => 'sole', 'water' => 'wasser', 'air' => 'luft', 'direct' => 'kollektor'][$source] ?? 'neutral';
    $sourceNames = [
        'sole' => 'Sole-Wasser-Wärmepumpe', 'water' => 'Wasser-Wasser-Wärmepumpe',
        'air' => 'Luft-Wasser-Wärmepumpe', 'direct' => 'Wärmepumpe mit Direktverdampfung',
    ];
    $sourceChip = [
        'sole' => 'Sole (Erdreich)', 'water' => 'Grundwasser', 'air' => 'Außenluft', 'direct' => 'Direktverdampfung',
    ][$source] ?? 'Wärmequelle';
    $manufacturers = [
        0 => 'Luxtronik', 1 => 'iDM Navigator', 3 => 'Shelly Pro3EM', 4 => 'Stiebel Eltron ISG',
        5 => 'Dimplex WPM', 6 => 'E3DC-Leistungsmesser',
    ];
    $manufacturer = $manufacturers[$wpType] ?? 'Wärmepumpe';

    // --- Verdichter und Betriebsart ---------------------------------------------------------
    $stage = ($wpType === 0 && is_array($json['luxtronik_operating_stage'] ?? null)) ? $json['luxtronik_operating_stage'] : null;
    $stageOk = is_array($stage) && ($stage['status'] ?? '') === 'OK';
    $running = null;
    $runningWhy = 'Verdichterzustand nicht belegt';
    if ($success) {
        if ($stageOk && is_bool($stage['compressor_running'] ?? null)) {
            $running = $stage['compressor_running'];
            $runningWhy = 'Luxtronik-Betriebsstufe';
        } elseif ($wpType === 0 && ($json['wp_compressor_observation_valid'] ?? null) === true && is_bool($json['wp_compressor_running'] ?? null)) {
            $running = $json['wp_compressor_running'];
            $runningWhy = 'Verdichterbeobachtung des Energy Managers';
        } elseif (array_key_exists('Verdichter_Ein', $d) || array_key_exists('Verdichter', $d)) {
            $running = !empty($d['Verdichter_Ein']) || !empty($d['Verdichter']);
            $runningWhy = 'Verdichtermeldung der Wärmepumpe';
        }
    }
    $modeText = trim((string)($d['Betriebszustand'] ?? ''));
    if ($modeText === '' && wpv_num($d['Betriebsart'] ?? null) !== null) {
        $modeText = [0 => 'Heizen', 1 => 'Warmwasser', 2 => 'Schwimmbad', 3 => 'E-Sperre', 4 => 'Abtauen', 5 => 'Standby'][(int)$d['Betriebsart']] ?? '';
    }
    $textMode = wpv_mode_from_text($modeText);
    if (!$success) $mode = 'unbekannt';
    elseif ($textMode === 'evu') $mode = 'evu';
    elseif ($textMode === 'abtauen') $mode = 'abtauen';
    elseif ($running === true) $mode = in_array($textMode, ['heizen', 'ww', 'kühlen'], true) ? $textMode : 'läuft';
    elseif ($running === false) $mode = 'bereit';
    else $mode = 'unbekannt';

    // --- Temperaturen ---------------------------------------------------------------------------
    $aussen = wpv_pick($d, ['Außentemperatur', 'Aussentemp']);
    $mittel = wpv_pick($d, ['Aussentemperatur_Mittel', 'Aussentemp_Mittel', 'Gemittelte Außentemperatur',
        'Gemittelte Aussentemperatur', 'Mitteltemperatur', 'Außentemperatur_Mittel', 'Aussen_Mittel']);
    $seasonBase = $mittel ?? wpv_num($d['wp_season_temp'] ?? null) ?? $aussen;
    $heizgrenze = wpv_pick($d, ['Heizgrenze_Temperatur', 'Heizgrenze', 'Heizgrenze_Temp']) ?? wpv_num($conf['heizgrenze_temp'] ?? null);
    $season = ($seasonBase !== null && $heizgrenze !== null) ? ($seasonBase < $heizgrenze ? 'winter' : 'sommer') : null;

    $vl = wpv_pick($d, ['Vorlauf_Ist', 'Vorlauf']);
    $vlSoll = ($wpType === 1) ? wpv_pick($d, ['Vorlauf_Soll', 'Vorlauf-Soll']) : null;
    $rl = wpv_pick($d, ['Ruecklauf_Ist', 'Rücklauf']);
    if ($wpType === 4) $rlSoll = wpv_pick($d, ['Heizkreis1_Soll', 'wp_heating_circuit_soll']);
    elseif ($wpType === 1) $rlSoll = null;
    else $rlSoll = wpv_pick($d, ['Ruecklauf_Soll', 'Rücklauf-Soll', 'Rückl.-Soll']);
    $ww = wpv_pick($d, ['Warmwasser_Ist', 'Warmwasser-Ist']);
    $wwSoll = wpv_pick($d, ['Warmwasser_Soll', 'Warmwasser-Soll']);

    // Wärmequelle (waermepumpe.php Z. 1482–1504); die Außentemperatur gilt nur bei Luft als Quelleintritt.
    $srcEin = null; $srcAus = null; $srcSingle = false;
    if ($wpType === 4) {
        $srcEin = wpv_pick($d, ['Quellentemperatur', 'Waermequelle_Temperatur', 'Wärmequelle_Temperatur']);
        $srcSingle = true;
    } elseif ($wpType === 5 || $wpType === 1) {
        $srcEin = ($source === 'air') ? wpv_pick($d, ['Zuluft', 'Außentemperatur', 'Aussentemp']) : wpv_pick($d, ['Zuluft']);
        $srcSingle = true;
    } else {
        $srcEin = wpv_pick($d, ['Sole_Ein', 'Wärmequelle-Ein', 'Waermequelle_Ein', 'Waermequelle-Ein', 'WQ_Eintritt', 'Zuluft']);
        if ($srcEin === null && $source === 'air') $srcEin = $aussen;
        $srcAus = wpv_pick($d, ['Sole_Aus', 'Wärmequelle-Aus', 'Waermequelle_Aus', 'Waermequelle-Aus', 'WQ_Austritt']);
    }

    // --- Leistung, COP, Arbeitszahlen (waermepumpe.php Z. 324–372, 623–668) -------------------
    $heizKw = wpv_pick($d, ['Leistung_Heiz_kW', 'Heizleistung Ist']);
    $heizEstimated = !empty($d['stiebel_heat_power_estimated']) || !empty($d['dimplex_heat_power_estimated']) || !empty($d['wp_heat_power_estimated']);
    $elecW = null;
    $elecWhy = 'Keine Leistungsmessung im Datensatz';
    if ($success && (isset($d['Leistung_Verdichter_W']) || isset($d['Leistungsaufnahme']))) {
        $elecW = wpv_num($d['Leistung_Verdichter_W'] ?? null);
        if ($elecW === null && wpv_num($d['Leistungsaufnahme'] ?? null) !== null) $elecW = wpv_num($d['Leistungsaufnahme']) * 1000;
        // Bestätigter Verdichterstillstand: gemessene Aufnahme 0 (waermepumpe.php Z. 332–334)
        if ($elecW !== null && $wpType !== 4 && empty($d['Verdichter_Ein']) && empty($d['Verdichter'])) $elecW = 0.0;
    } elseif ($success && $wpType === 6) {
        $elecW = wpv_num($d['WP_Power'] ?? null);
    } elseif ($success) {
        $elecW = is_numeric($in['history_wp_w'] ?? null) ? (float)$in['history_wp_w'] : null;
        $elecWhy = 'Kein WP-Wert im Live-Verlauf';
    }
    if ($success && $wpType === 4 && ($heizKw === null || $heizKw <= 0) && $elecW !== null && $elecW > 0 && $running === true) {
        $powerSource = strtolower((string)($d['stiebel_power_source'] ?? ''));
        $copEstimate = max(0.0, (float)($conf['stiebel_isg_cop_estimate'] ?? 3.0));
        if ($copEstimate > 0 && (strpos($powerSource, 'dhw') !== false || strpos($powerSource, 'heating') !== false)) {
            $heizKw = round($elecW * $copEstimate / 1000.0, 3);
            $heizEstimated = true;
        }
    }
    $cop = ($heizKw !== null && $heizKw > 0 && $elecW !== null && $elecW > 0) ? ($heizKw * 1000.0) / $elecW : null;
    $copWhy = ($running === false) ? 'Verdichter aus' : 'Heiz- oder Stromleistung fehlt';

    $taz = null; $tazLabel = 'Tages-AZ'; $tazNote = ''; $tazWhy = '';
    $jaz = null; $jazWhy = 'Energiezähler der Wärmepumpe fehlen';
    if ($wpType === 4) {
        $heatDay = wpv_num($d['Waerme_Tag_kWh'] ?? null);
        $elecDay = wpv_num($d['Strom_Tag_kWh'] ?? null);
        if ($heatDay !== null && $heatDay > 0 && $elecDay !== null && $elecDay > 0.05) $taz = $heatDay / $elecDay;
        $tazWhy = $why('Tageszähler der ISG fehlen oder noch zu klein');
        if ($heatDay !== null && $elecDay !== null) $tazNote = 'Tageszähler der ISG · ' . wpv_fmt($heatDay, 1, ' kWh') . ' Wärme / ' . wpv_fmt($elecDay, 1, ' kWh') . ' Strom';
    } elseif ($wpType === 1) {
        $tazWhy = 'Der iDM liefert keinen Stromzähler';
        $idmTotal = wpv_num($conf['idm_e_total'] ?? null);
        $idmHeat = wpv_num($d['Waermemenge_Gesamt_Kum'] ?? null);
        if ($idmHeat === null || $idmHeat <= 0) {
            $idmHeat = (wpv_pick($d, ['Waermemenge Heizen', 'Wärmemenge Heizen']) ?? 0.0) + (wpv_pick($d, ['Waermemenge Warmwasser', 'Wärmemenge Warmwasser']) ?? 0.0);
        }
        if ($idmTotal !== null && $idmTotal > 0 && $idmHeat > 0) $jaz = $idmHeat / $idmTotal;
        $jazWhy = 'Gesamt-JAZ nur mit idm_e_total in der Konfiguration';
    } elseif ($wpType !== 6) {
        $counters = e3dcHeatpumpEnergyCounters($d);
        $ratio = e3dcHeatpumpDayWorkRatio(is_array($in['day_start'] ?? null) ? $in['day_start'] : [], $counters);
        $taz = $ratio['ratio'];
        if ($ratio['since'] !== null && $ratio['since'] > '00:15') $tazLabel = 'Tages-AZ seit ' . $ratio['since'];
        $tazWhy = $why((string)$ratio['reason']);
        if ($ratio['heat_kwh'] !== null) {
            $tazNote = 'seit ' . ($ratio['since'] ?? '00:00') . ' · ' . wpv_fmt((float)$ratio['heat_kwh'], 1, ' kWh') . ' Wärme / ' . wpv_fmt((float)$ratio['elec_kwh'], 1, ' kWh') . ' Strom';
        }
        if ($counters['heat_kwh'] !== null && $counters['elec_kwh'] !== null && $counters['elec_kwh'] > 0) {
            $jaz = $counters['heat_kwh'] / $counters['elec_kwh'];
        }
    } else {
        $tazWhy = 'Der Leistungsmesser liefert keine Wärmemenge';
        $jazWhy = $tazWhy;
    }
    if ($jaz !== null && ($jaz <= 0 || $jaz > 15)) { $jaz = null; $jazWhy = 'Arbeitszahl unplausibel'; }

    // Verdichterfrequenz: Luxtronik-Betriebsstufe oder Herstellerfeld; beim Shelly ist es die Netzfrequenz.
    $hzIst = null; $hzSoll = null;
    if ($stageOk) {
        $hzIst = wpv_num($stage['frequency_hz'] ?? null);
        $hzSoll = wpv_num($stage['frequency_target_hz'] ?? null);
    }
    if ($hzIst === null && $wpType !== 3) $hzIst = ($wpType === 4) ? wpv_num($d['stiebel_compressor_hz'] ?? null) : wpv_pick($d, ['Freq_Ist', 'Freq. aktuell']);
    if ($hzSoll === null && in_array($wpType, [0, 5], true)) $hzSoll = wpv_pick($d, ['Freq_Soll', 'Freq. Sollwert']);
    $hzWhy = ($wpType === 3) ? 'Der Shelly misst die Netzfrequenz, nicht den Verdichter' : (($running === false) ? 'Verdichter aus' : 'Keine Frequenzmeldung');
    if ($hzIst !== null && $hzIst <= 0) $hzIst = null;
    if ($hzSoll !== null && $hzSoll <= 0) $hzSoll = null;

    // --- Pufferspeicher (Nutzerangabe wp_buffer_sensor) ----------------------------------------
    $bufferSensor = wpv_buffer_sensor($conf['wp_buffer_sensor'] ?? 'none');
    $buffer = null; $bufferWhy = ''; $bufferLabel = '';
    if ($bufferSensor === 'luxtronik_ruecklauf_extern') {
        $bufferLabel = 'Fühler: externer Rücklauf der Luxtronik';
        if ($wpType === 0) {
            $buffer = wpv_pick($d, ['Ruecklauf_Extern', 'Rückl.-Extern']);
            $bufferWhy = $why('Kein Wert für den externen Rücklauf im Datensatz');
        } else {
            $bufferWhy = 'Der gewählte Pufferfühler gehört zur Luxtronik; diese Anlage ist kein Luxtronik-Typ';
        }
    } elseif ($bufferSensor === 'stiebel_puffer') {
        $bufferLabel = 'Fühler: Pufferfühler der Stiebel-ISG';
        if ($wpType === 4) {
            $buffer = wpv_pick($d, ['Puffer_Ist']);
            $bufferWhy = $why('Kein Wert für den Pufferfühler im Datensatz');
        } else {
            $bufferWhy = 'Der gewählte Pufferfühler gehört zur Stiebel-ISG; diese Anlage ist kein Stiebel-Typ';
        }
    }
    $hasBuffer = $bufferSensor !== 'none';

    // --- Bewegung nur bei belegtem Zustand -------------------------------------------------------
    $compressorOn = ($running === true) && $mode !== 'evu';
    $flags = [
        'src' => $compressorOn,
        'wp' => $compressorOn,
        'hz' => $compressorOn && $mode === 'heizen',
        'ww' => $compressorOn && $mode === 'ww',
        // Mit Puffer fördert eine eigene Heizkreispumpe; ihr Signal ist nicht belegt, deshalb ruhig.
        'hk' => !$hasBuffer && $compressorOn && $mode === 'heizen',
    ];
    $hkText = 'Fußboden · ' . (($hasBuffer || $running === null || !$success) ? '--' : ($flags['hk'] ? 'aktiv' : 'aus'));
    $hkWhy = $hasBuffer ? 'Mit Pufferspeicher ist die Heizkreispumpe nicht gemessen' : (($running === null || !$success) ? $why('Verdichterzustand nicht belegt') : 'Aus Verdichter und Betriebsart Heizen abgeleitet');

    // --- Statusleiste ------------------------------------------------------------------------------
    $modeMeta = [
        'heizen' => ['Heizen', 'b-warn', '#f5a524', 'Verdichter läuft (Heizen)'],
        'ww' => ['Warmwasser', 'b-danger', '#ff5a5f', 'Verdichter läuft (Warmwasser)'],
        'kühlen' => ['Kühlen', 'b-info', '#22c3e6', 'Verdichter läuft (Kühlen)'],
        'läuft' => ['Betrieb', 'b-warn', '#f5a524', 'Verdichter läuft'],
        'abtauen' => ['Abtauen', 'b-info', '#22c3e6', 'Abtauen'],
        'bereit' => ['Bereit', 'b-muted', '#3ddc84', 'Bereit – Verdichter aus'],
        'evu' => ['EVU-Sperre', 'b-purple', '#b69cff', 'EVU-Sperre'],
        'unbekannt' => ['--', 'b-muted', '#9aa5b1', 'Zustand unbekannt'],
    ][$mode];
    $statusText = $modeMeta[3];
    $statusTitle = $success ? ('Quelle: ' . $runningWhy . ($modeText !== '' ? ' · Betriebszustand: ' . $modeText : '')) : $noData;
    if ($stageOk && $running === false && trim((string)($stage['label'] ?? '')) !== '' && !in_array($mode, ['evu', 'abtauen'], true)) {
        $statusText = trim((string)$stage['label']);
    }
    $extra = [];
    if (!empty($json['pv_pause_active'])) {
        $extra[] = ['b-danger', ((string)($json['pv_pause_owner'] ?? '') === 'source_recovery_heatpump') ? 'Quell-Erholung' : 'PV-Pause', 'Wärmepumpen-Pause des Energy Managers'];
    } elseif (!empty($json['boost_active'])) {
        $extra[] = ['b-ok', 'PV-Boost', 'PV-Überschuss aktiv'];
    }
    $wwSofort = is_array($json['manual_ww_sofort'] ?? null) ? $json['manual_ww_sofort'] : [];
    $wwSofortUntil = (float)($wwSofort['until_ts'] ?? 0);
    // „… wartet“: nur aus der Zurückhaltung des Kanalautomaten und dem Befehlszustand
    $wwHoldBadge = (!empty($in['flag_manual_ww']) && (!empty($wwSofort['active']) || !empty($wwSofort['pending'])))
        ? wpv_hold_badge($json, ['ww'], 'WW-Sofort', $now, $wwSofortUntil) : null;
    if ($wwHoldBadge !== null) {
        $extra[] = $wwHoldBadge;
    } elseif (!empty($in['flag_manual_ww'])) {
        if (!empty($wwSofort['active']) && $wwSofortUntil > $now) {
            $extra[] = ['b-danger', 'WW-Sofort bis ' . date('H:i', (int)$wwSofortUntil), 'Nutzerbefehl Warmwasser sofort'];
        } else {
            $extra[] = ['b-warn', 'WW-Sofort angefordert', 'Der Wärmepumpen-Manager führt den Befehl gerade nicht aus'];
        }
    }
    if (!empty($in['flag_manual_boost'])) {
        $extra[] = wpv_hold_badge($json, ['hz', 'ww'], 'Boost', $now) ?? ['b-warn', 'Boost aktiv', 'Manueller Boost'];
    }

    $window = static function ($from, $to) use ($now): ?array {
        $a = wpv_num($from); $b = wpv_num($to);
        if ($a === null || $b === null) return null;
        $fmt = static function (float $h): string {
            $hh = (int)floor($h); $mm = (int)round(($h - $hh) * 60);
            if ($mm === 60) { $hh++; $mm = 0; }
            return sprintf('%02d:%02d', $hh % 24, $mm);
        };
        $nowDec = (float)date('G', $now) + (float)date('i', $now) / 60.0;
        $inside = ($a <= $b) ? ($nowDec >= $a && $nowDec < $b) : ($nowDec >= $a || $nowDec < $b);
        return ['text' => $fmt($a) . '–' . $fmt($b), 'active' => $inside];
    };
    $timerOn = cfgBool($conf['ww_timer_enable'] ?? false, false);
    $wwWindow = $timerOn ? $window($conf['wwvon'] ?? null, $conf['wwbis'] ?? null) : null;
    $circWindow = $timerOn ? $window($conf['ww_circ_von'] ?? null, $conf['ww_circ_bis'] ?? null) : null;

    // --- Kopf und Verbindungsstatus ----------------------------------------------------------------
    if ($success) {
        $badge = ['b-ok', 'Verbunden', ''];
    } elseif ($wpType === 4 && ($in['stiebel_status'] ?? '') === 'stale') {
        $badge = ['b-warn', 'Veraltet', $noData];
    } else {
        $badge = ['b-danger', 'Fehler', $noData];
    }
    $autoMode = wpv_num($json['auto_mode'] ?? ($conf['auto_mode'] ?? null));

    // --- Szene: Standort gerundet, Wetter der aktuellen Stunde ---------------------------------
    $lat = wpv_num($conf['hoehe'] ?? null);   // „hoehe“ ist der Breitengrad
    $lon = wpv_num($conf['laenge'] ?? null);
    $locNote = '';
    if ($lat === null || $lon === null || ($lat == 0.0 && $lon == 0.0) || abs($lat) > 90 || abs($lon) > 180) {
        $lat = 51.2; $lon = 10.5;
        $locNote = 'Standort nicht eingestellt; Sonnenstand für die Mitte Deutschlands';
    }
    $weather = wpv_weather(is_array($in['weather_alerts'] ?? null) ? $in['weather_alerts'] : null, $now);
    $flowDur = $compressorOn ? max(0.7, min(2.2, 2.2 - ($heizKw ?? 0.0) * 0.25)) : 1.8;

    return [
        'wp_type' => $wpType,
        'success' => $success,
        'no_data' => $noData,
        'badge' => $badge,
        'age_s' => is_numeric($in['data_age_s'] ?? null) ? (int)$in['data_age_s'] : null,
        'data_time' => is_numeric($in['data_mtime'] ?? null) ? date('H:i:s', (int)$in['data_mtime']) : null,
        'auto_mode' => $autoMode === null ? null : (int)$autoMode,
        'subtitle' => ($sourceNames[$source] ?? 'Wärmepumpe (Wärmequelle nicht eingestellt)') . ' · ' . $manufacturer,
        'manufacturer' => $manufacturer,
        'source' => $source,
        'scene_source' => $sceneSource,
        'source_chip' => $sourceChip,
        'source_single' => $srcSingle,
        'circuit' => 'fbh',
        'mode' => $mode,
        'mode_meta' => $modeMeta,
        'mode_text' => $modeText,
        'status_text' => $statusText,
        'status_title' => $statusTitle,
        'running' => $running,
        'flags' => $flags,
        'flow_dur' => $flowDur,
        'season' => $season,
        'heizgrenze' => $heizgrenze,
        'buffer' => ['present' => $hasBuffer, 'sensor' => $bufferSensor, 'label' => $bufferLabel] + $val($buffer, $bufferWhy),
        'hk_text' => $hkText,
        'hk_why' => $hkWhy,
        'v' => [
            'aussen' => $val($aussen, $why('Kein Außenfühler im Datensatz')),
            'mittel' => $val($mittel, $why('Keine gemittelte Außentemperatur im Datensatz')),
            'vl' => $val($vl, $why('Kein Vorlauffühler im Datensatz')),
            'vl_soll' => $val($vlSoll, $why('Kein Vorlauf-Soll im Datensatz')),
            'rl' => $val($rl, $why('Kein Rücklauffühler im Datensatz')),
            'rl_soll' => $val($rlSoll, ($wpType === 1) ? 'Der iDM meldet kein Rücklauf-Soll' : $why('Kein Rücklauf-Soll im Datensatz')),
            'ww' => $val($ww, $why('Kein Warmwasserfühler im Datensatz')),
            'ww_soll' => $val($wwSoll, $why('Kein Warmwasser-Soll im Datensatz')),
            'src_ein' => $val($srcEin, $why('Kein Fühler am Eintritt der Wärmequelle im Datensatz')),
            'src_aus' => $val($srcAus, $srcSingle ? 'Diese Wärmepumpe meldet nur einen Quellwert' : $why('Kein Fühler am Austritt der Wärmequelle im Datensatz')),
            'heiz_kw' => $val($heizKw, $why('Keine Heizleistung im Datensatz')),
            'elec_kw' => $val($elecW === null ? null : $elecW / 1000.0, $why($elecWhy)),
            'cop' => $val($cop, $why($copWhy)),
            'taz' => $val($taz, $tazWhy !== '' ? $tazWhy : $why('Tages-AZ nicht berechenbar')),
            'jaz' => $val($jaz, $why($jazWhy)),
            'hz_ist' => $val($hzIst, $why($hzWhy)),
            'hz_soll' => $val($hzSoll, $why($hzWhy)),
        ],
        'heiz_estimated' => $heizEstimated,
        'taz_label' => $tazLabel,
        'taz_note' => $tazNote,
        'ww_window' => $wwWindow,
        'circ_window' => $circWindow,
        'extra_badges' => $extra,
        'actions' => [
            'show' => $success && $wpType !== 4,
            'boost_active' => !empty($in['flag_manual_boost']),
            'ww_active' => !empty($in['flag_manual_ww']),
            'ww_minutes' => ((int)($conf['ww_sofort_duration'] ?? 0) > 0) ? (int)$conf['ww_sofort_duration'] : 120,
        ],
        'ice' => $sceneSource === 'luft' && ($mode === 'abtauen' || ($aussen !== null && $aussen < 2)),
        'cop_class' => e3dcHeatpumpCopColorClass($cop, $source),
        'taz_class' => e3dcHeatpumpCopColorClass($taz, $source),
        'jaz_class' => e3dcHeatpumpCopColorClass($jaz, $source),
        'csrf_input' => '',
        'js' => [
            'lat' => round($lat, 1),
            'lon' => round($lon, 1),
            'locNote' => $locNote,
            'wx' => $weather['wx'],
            'wxWhy' => $weather['why'],
            'aussen' => $aussen,
            'ageS' => is_numeric($in['data_age_s'] ?? null) ? (int)$in['data_age_s'] : null,
            'timeOverrideMs' => null,
        ],
    ];
}

/** Rohrleitungen wie im Prototyp: [Gruppe, Farbe, Bewegungsflag, Pfad]. */
function wpv_pipes(): array
{
    $wave = 'M932 372';
    for ($x = 934; $x <= 1152; $x += 2) {
        $wave .= ' L' . $x . ' ' . sprintf('%.1f', 372 + 3 * sin(($x - 932) / 6));
    }
    return [
        ['sole', 'src-cold', 'src', 'M612 310 H566 V400 H440 V592'],
        ['sole', 'src-warm', 'src', 'M440 592 A8 8 0 0 0 456 592 V412 H578 V340 H612'],
        ['kollektor', 'src-cold', 'src', 'M612 310 H566 V400 H470 V428'],
        ['kollektor', 'src-cold', 'src', 'M470 428 H90 A9 9 0 0 0 90 446 H470 A9 9 0 0 1 470 464 H90 A9 9 0 0 0 90 482 H490'],
        ['kollektor', 'src-warm', 'src', 'M490 482 V412 H578 V340 H612'],
        ['wasser', 'src-cold', 'src', 'M612 310 H566 V400 H250 V572'],
        ['wasser', 'src-warm', 'src', 'M440 572 V412 H578 V340 H612'],
        ['luft', 'hot', 'src', 'M542 310 H612'],
        ['luft', 'cold', 'src', 'M612 340 H542'],
        ['neutral', 'src-warm', 'src', 'M542 310 H612'],
        ['neutral', 'src-cold', 'src', 'M612 340 H542'],
        ['all', 'hot', 'wp', 'M682 300 H710'],
        ['all', 'hot', 'ww', 'M734 300 H752'],
        ['all', 'cold', 'ww', 'M752 356 H682'],
        ['buf', 'hot', 'hz', 'M722 288 V236 H857 V262'],
        ['buf', 'cold', 'hz', 'M832 368 H682'],
        ['buf', 'hot', 'hk', 'M882 290 H915'],
        ['buf', 'cold', 'hk', 'M915 352 H882'],
        ['nobuf', 'hot', 'hz', 'M722 288 V236 H896 V290 H915'],
        ['nobuf', 'cold', 'hz', 'M915 352 H840 V368 H682'],
        ['fbh', 'hot', 'hk', 'M915 290 H932 V372'],
        ['fbh', 'hot', 'hk', $wave],
        ['fbh', 'cold', 'hk', 'M1152 372 V378 H924 V352 H915'],
        ['hk', 'hot', 'hk', 'M915 290 H1046 V300 H1052'],
        ['hk', 'cold', 'hk', 'M1144 326 H1154 V358 H924 V352 H915'],
        ['coil', 'hot coil', 'ww', 'M752 300 H790 A5.5 5.5 0 0 1 790 311 H762 A5.5 5.5 0 0 0 762 322 H790 A5.5 5.5 0 0 1 790 333 H762 A5.5 5.5 0 0 0 762 344 H790 A6 6 0 0 1 790 356 H752'],
    ];
}

/** SVG-Text mit Tooltip, wenn der Wert fehlt. */
function wpv_svg_text(string $attrs, string $text, string $why = ''): string
{
    return '<text ' . $attrs . '>' . ($why !== '' ? '<title>' . wpv_h($why) . '</title>' : '') . wpv_h($text) . '</text>';
}

/** Kachel im Werteraster. */
function wpv_tile(string $label, string $valueHtml, string $extraHtml = '', string $attrs = ''): string
{
    return '<div class="val"' . $attrs . '><div class="l">' . $label . '</div><div class="v">' . $valueHtml . '</div>' . $extraHtml . '</div>';
}

function wpv_span(array $value, int $decimals, string $unit, string $class = '', string $id = ''): string
{
    $text = ($value['v'] === null) ? '--' : wpv_fmt($value['v'], $decimals, $unit);
    return '<span' . ($id !== '' ? ' id="' . $id . '"' : '') . ($class !== '' ? ' class="' . $class . '"' : '')
        . ($value['why'] !== '' ? ' title="' . wpv_h($value['why']) . '"' : '') . '>' . wpv_h($text) . '</span>';
}

/** Darstellung. Erwartet den Zustand aus wpv_build_state(); liest und schreibt nichts. */
function wpv_render(array $s): string
{
    $v = $s['v'];
    $flags = $s['flags'];
    $scene = $s['scene_source'];
    $luftLike = in_array($scene, ['luft', 'neutral'], true);
    $buffer = $s['buffer'];
    $visible = [
        'sole' => $scene === 'sole', 'kollektor' => $scene === 'kollektor', 'wasser' => $scene === 'wasser',
        'luft' => $scene === 'luft', 'neutral' => $scene === 'neutral',
        'buf' => $buffer['present'], 'nobuf' => !$buffer['present'],
        'fbh' => $s['circuit'] === 'fbh', 'hk' => $s['circuit'] === 'hk', 'all' => true, 'coil' => true,
    ];
    $pipes = ''; $coil = '';
    foreach (wpv_pipes() as [$group, $kind, $flag, $path]) {
        if (empty($visible[$group])) continue;
        $g = '<g class="pipe-g' . (!empty($flags[$flag]) ? ' on' : '') . '" data-pipe="' . $group . '-' . $flag . '">'
            . '<path class="pipe ' . $kind . '" d="' . $path . '"/><path class="flow" d="' . $path . '"/></g>';
        if ($group === 'coil') $coil .= $g; else $pipes .= $g;
    }
    $f1 = static function (array $value, string $unit = '', int $dec = 1): string {
        return $value['v'] === null ? '--' : wpv_fmt($value['v'], $dec, $unit);
    };
    $ww = $v['ww']; $wwSoll = $v['ww_soll'];
    $wwTop = $ww['v'] !== null ? wpv_temp_color($ww['v']) : '#8b95a2';
    $wwBot = $ww['v'] !== null ? wpv_temp_color($ww['v'] - 12) : '#6b7480';
    $bufTop = $buffer['v'] !== null ? wpv_temp_color($buffer['v']) : '#8b95a2';
    $bufBot = $buffer['v'] !== null ? wpv_temp_color($buffer['v'] - 6) : '#6b7480';
    $meta = $s['mode_meta'];
    $running = $s['running'];
    $srcText = $s['source_single']
        ? $f1($v['src_ein'], ' °C')
        : 'ein ' . $f1($v['src_ein']) . ' → aus ' . $f1($v['src_aus']) . (($v['src_ein']['v'] ?? $v['src_aus']['v']) === null ? '' : ' °C');
    $srcWhy = trim($v['src_ein']['why'] . ' ' . $v['src_aus']['why']);
    if ($running === true && $v['src_ein']['v'] !== null && $v['src_aus']['v'] !== null) {
        $srcSub = 'Spreizung ' . wpv_fmt($v['src_ein']['v'] - $v['src_aus']['v'], 1, ' K');
    } elseif ($running === false) {
        $srcSub = 'Verdichter aus';
    } else {
        $srcSub = ' ';
    }
    $heizText = ($s['heiz_estimated'] && $v['heiz_kw']['v'] !== null ? 'ca. ' : '') . $f1($v['heiz_kw'], ' kW');
    $wpSub = ($v['elec_kw']['v'] === null ? '--' : wpv_fmt($v['elec_kw']['v'], 2)) . ' kW el. · COP ' . ($v['cop']['v'] === null ? '--' : wpv_fmt($v['cop']['v'], 1));
    $vlText = $f1($v['vl']) . ' / ' . $f1($v['rl']) . (($v['vl']['v'] ?? $v['rl']['v']) === null ? '' : ' °C');
    $vlSub = ($s['wp_type'] === 1)
        ? 'Vorlauf-Soll ' . $f1($v['vl_soll'], ' °C')
        : 'Rücklauf-Soll ' . $f1($v['rl_soll'], ' °C');
    $vlSubWhy = ($s['wp_type'] === 1) ? $v['vl_soll']['why'] : $v['rl_soll']['why'];
    $hzIst = $v['hz_ist']['v'] === null ? '--' : wpv_fmt($v['hz_ist']['v'], 0, ' Hz');
    $hzSoll = $v['hz_soll']['v'] === null ? '--' : wpv_fmt($v['hz_soll']['v'], 0, ' Hz');
    $wwNote = '';
    if ($s['ww_window'] !== null) {
        $wwNote = $s['ww_window']['active'] ? 'im Warmwasser-Fenster' : 'außerhalb des Warmwasser-Fensters';
    }
    // Verweise bleiben in der aufrufenden Oberfläche (index.php oder mobile.php)
    $entry = e3dcWpViewEntrypoint();
    $action = $entry . '?seite=waermepumpe';
    $ajaxUrl = $entry . '?seite=waermepumpe_vorschau&ajax=1';
    $js = $s['js'];
    $jsJson = json_encode($js, JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT | JSON_UNESCAPED_UNICODE | JSON_PARTIAL_OUTPUT_ON_ERROR);
    ob_start();
    ?>
<style>
#wpv-root{
  --bg:#eef1f5; --surface:#ffffff; --surface-2:#f5f7fa; --border:#d5dbe3; --text:#1b2530; --muted:#5d6978;
  --accent:#0b89a8; --accent-soft:rgba(11,137,168,.12);
  --warn:#a86a00; --warn-soft:#fff4dc; --warn-line:#e0a526;
  --danger:#c0392b; --danger-soft:#fdecea; --danger-line:#e06a5f;
  --ok:#1a7f52; --ok-soft:#e3f4ec; --ok-line:#3fae7a;
  --info:#0b7fa8; --info-soft:#e2f3f9;
  --purple:#6b4bb8; --purple-soft:#efe9fb;
  --hot:#e5484d; --cold:#2f7fe0; --src-warm:#14a79d; --src-cold:#3b6fe0;
  --chip-bg:rgba(255,255,255,.9); --chip-line:rgba(27,37,48,.14); --chip-text:#1b2530; --chip-muted:#5d6978;
  --label-halo:rgba(255,255,255,.75);
  --soil-1:#b89472; --soil-2:#a8835f; --soil-3:#977350; --gw:rgba(90,160,235,.45); --pebble:rgba(80,60,40,.25); --bore:rgba(210,190,160,.55);
  --room:#fbfaf7; --wall-inner:#d9dde3; --slab:#b9bec6; --screed:#ddd6cb;
  --metal-1:#f7f9fb; --metal-2:#d3d9e0; --cab-line:#9aa4b1; --tank-line:#8b95a2;
  --furniture:#c9b9a6; --furniture-2:#b6a591; --lamp:#6b7480;
  --pad:#a9adb3; --unit:#e9edf1; --unit-line:#9aa4b1; --grille:#7c8794; --grille-bg:#cfd6de;
  --moon-dark:#9aa6c9; --rain:#6f8fb0; --win-frame:#8b95a2;
  --shadow:0 1px 2px rgba(16,24,40,.06), 0 4px 16px rgba(16,24,40,.06);
}
[data-bs-theme="dark"] #wpv-root,[data-theme="dark"] #wpv-root{
  --bg:#15171a; --surface:#212529; --surface-2:#2a2f34; --border:#3a4148; --text:#e8ecef; --muted:#9aa5b1;
  --accent:#22c3e6; --accent-soft:rgba(34,195,230,.12);
  --warn:#ffc107; --warn-soft:rgba(255,193,7,.07); --warn-line:#c99a06;
  --danger:#ff6b6b; --danger-soft:rgba(255,107,107,.07); --danger-line:#c9505a;
  --ok:#3ddc84; --ok-soft:rgba(61,220,132,.08); --ok-line:#2e9e62;
  --info:#22c3e6; --info-soft:rgba(34,195,230,.1);
  --purple:#b69cff; --purple-soft:rgba(182,156,255,.1);
  --hot:#ff5a5f; --cold:#4da3ff; --src-warm:#2dd4bf; --src-cold:#5b8cff;
  --chip-bg:rgba(16,21,28,.84); --chip-line:rgba(255,255,255,.14); --chip-text:#eef3f7; --chip-muted:#a4afbb;
  --label-halo:rgba(0,0,0,.55);
  --soil-1:#5a4531; --soil-2:#4c3a29; --soil-3:#3f3022; --gw:rgba(47,111,179,.5); --pebble:rgba(0,0,0,.25); --bore:rgba(120,105,85,.55);
  --room:#2a2f36; --wall-inner:#4a515b; --slab:#555c66; --screed:#4a4640;
  --metal-1:#c3cad2; --metal-2:#8a939e; --cab-line:#5f6873; --tank-line:#6b7480;
  --furniture:#6d5f52; --furniture-2:#5c5046; --lamp:#a9b1bb;
  --pad:#4b5058; --unit:#b9c0c8; --unit-line:#6b7480; --grille:#4a525c; --grille-bg:#8d96a1;
  --moon-dark:#2b3350; --rain:#9fb8d6; --win-frame:#6b7480;
  --shadow:0 1px 2px rgba(0,0,0,.3), 0 6px 20px rgba(0,0,0,.25);
}
#wpv-root{color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,Ubuntu,sans-serif;max-width:1280px;margin:0 auto 16px}
#wpv-root *{box-sizing:border-box}
#wpv-root .wpv-card{background:var(--surface);border:1px solid var(--border);border-radius:16px;box-shadow:var(--shadow);overflow:hidden}
#wpv-root .head{display:flex;flex-wrap:wrap;gap:12px 20px;align-items:center;justify-content:space-between;padding:14px 18px}
#wpv-root .title{display:flex;gap:12px;align-items:center}
#wpv-root .title h2{margin:0;font-size:22px;font-weight:700;line-height:1.2;color:var(--accent);letter-spacing:.01em}
#wpv-root .title .sub{color:var(--muted);font-size:13px}
#wpv-root .badges{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
#wpv-root .wpv-badge{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:700;line-height:1.45;padding:3px 10px;border-radius:999px;border:1px solid transparent}
#wpv-root .b-ok{background:var(--ok-soft);color:var(--ok);border-color:var(--ok-line)}
#wpv-root .b-warn{background:var(--warn-soft);color:var(--warn);border-color:var(--warn-line)}
#wpv-root .b-danger{background:var(--danger-soft);color:var(--danger);border-color:var(--danger-line)}
#wpv-root .b-info{background:var(--info-soft);color:var(--info);border-color:var(--info)}
#wpv-root .b-purple{background:var(--purple-soft);color:var(--purple);border-color:var(--purple)}
#wpv-root .b-muted{background:var(--surface-2);color:var(--muted);border-color:var(--border)}
#wpv-root .dot{width:8px;height:8px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 0 var(--ok);animation:wpv-pulse 2s infinite}
@keyframes wpv-pulse{0%{box-shadow:0 0 0 0 rgba(61,220,132,.5)}70%{box-shadow:0 0 0 7px rgba(61,220,132,0)}100%{box-shadow:0 0 0 0 rgba(61,220,132,0)}}
#wpv-root .wpv-alert{margin:0 18px 14px;padding:8px 12px;border-radius:10px;font-size:13px;border:1px solid var(--danger-line);background:var(--danger-soft);color:var(--text)}
#wpv-root .wpv-flash{margin:0 0 12px;padding:10px 14px;border-radius:12px;font-size:14px;font-weight:600;border:1px solid var(--ok-line);background:var(--ok-soft);color:var(--ok)}
#wpv-root .actions{display:grid;grid-template-columns:1fr 1fr auto;gap:10px;padding:0 18px 14px}
#wpv-root .actions form{display:contents}
#wpv-root .act{font:700 13.5px system-ui,sans-serif;letter-spacing:.03em;text-transform:uppercase;padding:10px 14px;border-radius:10px;border:1.5px solid;background:transparent;cursor:pointer;display:flex;gap:8px;align-items:center;justify-content:center}
#wpv-root .act.warn{color:var(--warn);border-color:var(--warn-line);background:var(--warn-soft)}
#wpv-root .act.danger{color:var(--danger);border-color:var(--danger-line);background:var(--danger-soft)}
#wpv-root .act.ok{color:var(--ok);border-color:var(--ok-line);background:var(--ok-soft)}
#wpv-root .act.muted{color:var(--muted);border-color:var(--border);background:var(--surface-2)}
#wpv-root .act.stop{color:#fff;border-color:var(--danger-line);background:var(--danger-line)}
#wpv-root .info-line{padding:0 18px 14px;font-size:13px;color:var(--muted)}
#wpv-root .scene-wrap{position:relative;border-top:1px solid var(--border);border-bottom:1px solid var(--border);background:#0b1020}
#wpv-root .wpv-scene{display:block;width:100%;height:auto}
#wpv-root .legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12px;color:var(--muted);padding:8px 18px}
#wpv-root .legend i{display:inline-block;width:18px;height:4px;border-radius:2px;vertical-align:middle;margin-right:6px}
#wpv-root .statusbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:12px 18px;border-bottom:1px solid var(--border)}
#wpv-root .pill{font-size:12.5px;padding:3px 10px;border-radius:999px;border:1px solid var(--border);color:var(--muted);background:var(--surface-2)}
#wpv-root .pill b{color:var(--text)}
#wpv-root .tabs{display:flex;gap:4px;padding:10px 14px 0;flex-wrap:wrap}
#wpv-root .tab{font:600 13.5px system-ui,sans-serif;padding:8px 14px;border-radius:10px 10px 0 0;border:1px solid transparent;border-bottom:none;background:transparent;color:var(--muted);cursor:default}
#wpv-root .tab[aria-selected=true]{background:var(--surface-2);color:var(--text);border-color:var(--border)}
#wpv-root .panel{background:var(--surface-2);border-top:1px solid var(--border);padding:16px 18px;border-radius:0 0 16px 16px}
#wpv-root .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px}
#wpv-root .val{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:10px 12px;text-align:center}
#wpv-root .val .l{font-size:12.5px;color:var(--muted)}
#wpv-root .val .v{font-size:19px;font-weight:700;margin-top:2px}
#wpv-root .val .s{font-size:12.5px;color:var(--muted);font-weight:400}
#wpv-root .t-hot{color:var(--hot)}#wpv-root .t-cold{color:var(--cold)}#wpv-root .t-ww{color:#f0a23a}#wpv-root .t-ok{color:var(--ok)}#wpv-root .t-src{color:var(--src-warm)}
#wpv-root .note{font-size:12.5px;color:var(--muted);margin:10px 0 0}
#wpv-root .foot{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 18px;font-size:12.5px;color:var(--muted);flex-wrap:wrap}
#wpv-root .pipe{fill:none;stroke-width:6;stroke-linecap:round;stroke-linejoin:round}
#wpv-root .pipe.hot{stroke:var(--hot)}#wpv-root .pipe.cold{stroke:var(--cold)}#wpv-root .pipe.src-warm{stroke:var(--src-warm)}#wpv-root .pipe.src-cold{stroke:var(--src-cold)}
#wpv-root .pipe.coil{stroke-width:3.5}
#wpv-root .flow{fill:none;stroke:rgba(255,255,255,.8);stroke-width:2.2;stroke-linecap:round;stroke-dasharray:3 11;opacity:0}
#wpv-root .pipe-g.on .flow{opacity:1;animation:wpv-flow var(--flow-dur,1.4s) linear infinite}
#wpv-root .pipe-g:not(.on) .pipe{opacity:.35}
@keyframes wpv-flow{to{stroke-dashoffset:-14}}
#wpv-root .hill-far{fill:var(--hill-far)}#wpv-root .hill-near{fill:var(--hill-near)}#wpv-root .grass{fill:var(--grass)}
#wpv-root .soil1{fill:var(--soil-1)}#wpv-root .soil2{fill:var(--soil-2)}#wpv-root .soil3{fill:var(--soil-3)}#wpv-root .gw{fill:var(--gw)}#wpv-root .pebble{fill:var(--pebble)}#wpv-root .bore{fill:var(--bore)}
#wpv-root .wall{fill:var(--wall);stroke:rgba(128,128,128,.35);stroke-width:1.5}#wpv-root .roof{fill:var(--roof);stroke:rgba(128,128,128,.35);stroke-width:1.5}#wpv-root .room{fill:var(--room)}#wpv-root .wall-inner{fill:var(--wall-inner)}#wpv-root .slab{fill:var(--slab)}#wpv-root .screed{fill:var(--screed)}
#wpv-root .cabinet{fill:url(#wpvMetal);stroke:var(--cab-line);stroke-width:1.2}
#wpv-root .tank{stroke:var(--tank-line);stroke-width:1.5}
#wpv-root .display{fill:#0f1a14}#wpv-root .display-text{font:600 9px ui-monospace,Consolas,monospace;fill:#7cffb2}
#wpv-root .cab-label{font:600 9.5px system-ui,sans-serif;fill:#2c3440}
#wpv-root .grille-line{stroke:var(--cab-line);stroke-width:1.5}
#wpv-root .unit{fill:var(--unit);stroke:var(--unit-line);stroke-width:1.2}#wpv-root .pad{fill:var(--pad)}
#wpv-root .grille-ring{fill:var(--grille-bg);stroke:var(--grille);stroke-width:3}
#wpv-root .blades path{fill:var(--grille)}#wpv-root .blades{transform-box:fill-box;transform-origin:center}
#wpv-root .fan-on .blades{animation:wpv-spin .7s linear infinite}
@keyframes wpv-spin{to{transform:rotate(360deg)}}
#wpv-root .vent{stroke:var(--unit-line);stroke-width:2}
#wpv-root .air path{fill:none;stroke:var(--src-cold);stroke-width:3;stroke-linecap:round;stroke-linejoin:round;opacity:0}
#wpv-root .fan-on .air path{animation:wpv-air 1.6s linear infinite}
@keyframes wpv-air{0%{opacity:0;transform:translateX(-16px)}30%{opacity:.9}100%{opacity:0;transform:translateX(18px)}}
#wpv-root .gwa path{fill:none;stroke:var(--src-warm);stroke-width:2;stroke-linecap:round;opacity:0}
#wpv-root .gw-on .gwa path{animation:wpv-gwflow 3.6s linear infinite}
@keyframes wpv-gwflow{0%{opacity:0;transform:translateX(-20px)}40%{opacity:.7}100%{opacity:0;transform:translateX(30px)}}
#wpv-root .heat path{fill:none;stroke:var(--hot);stroke-width:2;stroke-linecap:round;opacity:0}
#wpv-root .hk-on .heat path{animation:wpv-shimmer 2.8s ease-in-out infinite}
@keyframes wpv-shimmer{0%{opacity:0;transform:translateY(6px)}50%{opacity:.55}100%{opacity:0;transform:translateY(-12px)}}
#wpv-root .drift{animation:wpv-drift 80s ease-in-out infinite alternate}
@keyframes wpv-drift{from{transform:translateX(-35px)}to{transform:translateX(35px)}}
#wpv-root .cloud use{fill:var(--cloud)}
#wpv-root .drop{stroke:var(--rain);stroke-width:1.6;stroke-linecap:round;animation:wpv-fall .9s linear infinite}
@keyframes wpv-fall{0%{transform:translate(0,-40px);opacity:0}15%{opacity:.85}100%{transform:translate(-10px,40px);opacity:0}}
#wpv-root .flake{fill:#fff;animation:wpv-snowfall 7s linear infinite}
@keyframes wpv-snowfall{0%{transform:translate(0,-60px);opacity:0}10%{opacity:.95}100%{transform:translate(14px,90px);opacity:0}}
#wpv-root .lbl{font:600 11px system-ui,sans-serif;fill:var(--chip-text);paint-order:stroke;stroke:var(--label-halo);stroke-width:3px}
#wpv-root .chip .bg{fill:var(--chip-bg);stroke:var(--chip-line)}
#wpv-root .c-lbl{font:600 11px system-ui,sans-serif;fill:var(--chip-muted);letter-spacing:.02em}
#wpv-root .c-val{font:700 18px system-ui,sans-serif;fill:var(--chip-text)}
#wpv-root .c-val.sm{font-size:14px}
#wpv-root .c-sub{font:500 11.5px system-ui,sans-serif;fill:var(--chip-muted)}
#wpv-root .tank-lbl{font:600 9.5px system-ui,sans-serif;fill:#fff;paint-order:stroke;stroke:rgba(0,0,0,.4);stroke-width:2.5px}
#wpv-root .tank-val{font:700 13.5px system-ui,sans-serif;fill:#fff;paint-order:stroke;stroke:rgba(0,0,0,.45);stroke-width:3px}
#wpv-root .season rect{fill:var(--info-soft);stroke:var(--info)}#wpv-root .season text{font:700 10.5px system-ui,sans-serif;fill:var(--info)}
#wpv-root .season.sommer rect{fill:var(--warn-soft);stroke:var(--warn-line)}#wpv-root .season.sommer text{fill:var(--warn)}
#wpv-root .win-frame{fill:var(--win-frame)}
#wpv-root .sofa{fill:var(--furniture)}#wpv-root .sofa2{fill:var(--furniture-2)}
#wpv-root .lamp-stand{stroke:var(--lamp);stroke-width:3}#wpv-root .lamp-shade{fill:#e9dcc0}
#wpv-root .radiator rect{fill:#eef1f4;stroke:#aab3bd}
#wpv-root .valve polygon{stroke:var(--cab-line);stroke-width:1.2;fill:var(--surface)}
#wpv-root .valve polygon.on{fill:var(--accent)}
#wpv-root .ice path{stroke:#bfe9ff;stroke-width:2;stroke-linecap:round;fill:none}
@media (prefers-reduced-motion:reduce){#wpv-root .wpv-scene *{animation:none!important}}
#wpv-root.paused .wpv-scene *{animation-play-state:paused!important}
@media (max-width:900px){#wpv-root .wpv-scene .chip.opt{display:none}}
@media (max-width:640px){#wpv-root .wpv-scene .chip{display:none}#wpv-root .actions{grid-template-columns:1fr}#wpv-root .title h2{font-size:19px}}
</style>
<div id="wpv-root">
<?php if (!empty($s['flash'])): ?>
<div class="wpv-flash" id="wpvFlash" role="status"><?= wpv_h($s['flash']) ?></div>
<?php endif; ?>
<div class="wpv-card" id="wpv-card">
  <script type="application/json" id="wpv-state"><?= $jsJson ?></script>
  <div class="head">
    <div class="title">
      <svg width="30" height="30" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2c3 4.2 6 7.6 6 11.3A6 6 0 0 1 6 13.3C6 9.6 9 6.2 12 2z" fill="var(--accent)"/><path d="M12 9.5c1.4 2 2.8 3.5 2.8 5.2a2.8 2.8 0 0 1-5.6 0c0-1.7 1.4-3.2 2.8-5.2z" fill="var(--surface)" opacity=".55"/></svg>
      <div>
        <h2>Wärmepumpe</h2>
        <div class="sub"><?= wpv_h($s['subtitle']) ?></div>
      </div>
    </div>
    <div class="badges">
      <span class="wpv-badge <?= wpv_h($s['badge'][0]) ?>"<?= $s['badge'][2] !== '' ? ' title="' . wpv_h($s['badge'][2]) . '"' : '' ?>><?php if ($s['badge'][0] === 'b-ok'): ?><span class="dot"></span><?php endif; ?><?= wpv_h($s['badge'][1]) ?></span>
      <span class="wpv-badge b-muted" id="wpvAge"<?= $s['age_s'] === null ? ' title="Datenalter unbekannt"' : '' ?>><?= $s['age_s'] === null ? 'Datenalter --' : 'Daten vor ' . (int)$s['age_s'] . ' s' ?></span>
      <?php if ($s['auto_mode'] === 0): ?><span class="wpv-badge b-muted">Automatik aus</span><?php endif; ?>
    </div>
  </div>
  <?php if (!$s['success']): ?>
  <div class="wpv-alert" role="status"><?= wpv_h($s['no_data']) ?>. Alle Werte stehen deshalb auf „--“.</div>
  <?php endif; ?>
  <?php if ($s['actions']['show']): ?>
  <div class="actions">
    <form method="post" action="<?= wpv_h($action) ?>">
      <?= $s['csrf_input'] ?>
      <input type="hidden" name="wpv_origin" value="neu">
      <?php if ($s['actions']['boost_active']): ?>
      <button type="submit" name="manual_boost" value="off" class="act stop" title="Manuellen Boost beenden">⚡ Boost stoppen</button>
      <?php else: ?>
      <button type="submit" name="manual_boost" value="on" class="act warn" title="Manueller Boost über die bisherige Wärmepumpen-Seite">⚡ Boost (Akku leeren)</button>
      <?php endif; ?>
      <?php if ($s['actions']['ww_active']): ?>
      <button type="submit" name="manual_ww" value="off" class="act stop" title="Warmwasser sofort beenden">🚿 WW-Sofort stoppen</button>
      <?php else: ?>
      <button type="submit" name="manual_ww" value="on" class="act danger" title="<?= wpv_h('Warmwasser sofort für ' . (int)$s['actions']['ww_minutes'] . ' Minuten auf die Boost-Solltemperatur') ?>">🚿 1× Warmwasser</button>
      <?php endif; ?>
    </form>
    <form method="post" action="<?= wpv_h($action) ?>">
      <?= $s['csrf_input'] ?>
      <input type="hidden" name="wpv_origin" value="neu">
      <?php if ($s['auto_mode'] === 0): ?>
      <button type="submit" name="toggle_auto_mode" value="1" class="act muted" title="PV-Überschuss-Automatik einschalten">⏸ Automatik aus</button>
      <?php else: ?>
      <button type="submit" name="toggle_auto_mode" value="0" class="act ok" title="PV-Überschuss-Automatik abschalten">✓ Automatik</button>
      <?php endif; ?>
    </form>
  </div>
  <?php elseif ($s['wp_type'] === 4): ?>
  <div class="info-line">Stiebel ISG: Diese Anbindung liest nur Messwerte; Boost und Warmwasser-Knöpfe gibt es hier nicht.</div>
  <?php endif; ?>

  <div class="scene-wrap">
  <svg id="wpvScene" class="wpv-scene<?= ($scene === 'luft' && $flags['src']) ? ' fan-on' : '' ?><?= ($scene === 'wasser' && $flags['src']) ? ' gw-on' : '' ?><?= $flags['hk'] ? ' hk-on' : '' ?>" viewBox="<?= $luftLike ? '0 0 1200 470' : '0 0 1200 620' ?>" style="--flow-dur:<?= sprintf('%.2f', $s['flow_dur']) ?>s" role="img" aria-labelledby="wpvSceneTitle wpvSceneDesc">
    <title id="wpvSceneTitle">Anlagenbild der Wärmepumpe</title>
    <desc id="wpvSceneDesc">Schematische Darstellung von Wärmequelle, Wärmepumpe, Warmwasser- und Pufferspeicher sowie Heizkreis, mit Tageszeit und Wetter im Hintergrund. Die Werte stehen zusätzlich in den Karten unter dem Bild.</desc>
    <defs>
      <linearGradient id="wpvSkyGrad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" style="stop-color:var(--sky-top)"/>
        <stop offset=".6" style="stop-color:var(--sky-mid)"/>
        <stop offset="1" style="stop-color:var(--sky-bot)"/>
      </linearGradient>
      <radialGradient id="wpvSunGlow"><stop offset="0" stop-color="#fff6c4" stop-opacity=".95"/><stop offset=".35" stop-color="#ffd766" stop-opacity=".45"/><stop offset="1" stop-color="#ffd766" stop-opacity="0"/></radialGradient>
      <radialGradient id="wpvLampGlow"><stop offset="0" stop-color="#ffe3a3" stop-opacity=".9"/><stop offset="1" stop-color="#ffe3a3" stop-opacity="0"/></radialGradient>
      <linearGradient id="wpvMetal" x1="0" x2="1"><stop offset="0" style="stop-color:var(--metal-1)"/><stop offset="1" style="stop-color:var(--metal-2)"/></linearGradient>
      <linearGradient id="wpvGradWW" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="<?= $wwTop ?>"/><stop offset="1" stop-color="<?= $wwBot ?>"/></linearGradient>
      <linearGradient id="wpvGradBuf" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="<?= $bufTop ?>"/><stop offset="1" stop-color="<?= $bufBot ?>"/></linearGradient>
      <symbol id="wpvCloudShape" viewBox="0 0 140 70"><circle cx="38" cy="44" r="22"/><circle cx="70" cy="30" r="28"/><circle cx="104" cy="44" r="21"/><rect x="26" y="42" width="92" height="26" rx="13"/></symbol>
      <clipPath id="wpvSkyClip"><rect x="0" y="0" width="1200" height="388"/></clipPath>
    </defs>

    <!-- Himmel -->
    <rect x="0" y="0" width="1200" height="620" fill="url(#wpvSkyGrad)"/>
    <g id="wpvStars"></g>
    <g id="wpvSun" opacity="0"><circle r="80" fill="url(#wpvSunGlow)"/><circle id="wpvSunDisk" r="22" fill="#ffd766"/></g>
    <g id="wpvMoon" opacity="0"><circle r="16" style="fill:var(--moon-dark)"/><path id="wpvMoonLit" fill="#f3efe2"/></g>
    <g id="wpvClouds"></g>
    <rect id="wpvVeil" x="0" y="0" width="1200" height="400" style="fill:var(--veil)" opacity="0"/>

    <!-- Landschaft -->
    <path class="hill-far" d="M0 318 C110 282 240 296 360 312 S620 268 770 296 S1030 276 1200 300 V400 H0 Z"/>
    <path class="hill-near" d="M0 352 C140 326 290 344 410 346 S690 326 850 344 S1090 334 1200 348 V400 H0 Z"/>
    <g id="wpvPrecip" clip-path="url(#wpvSkyClip)"><g id="wpvRain" style="display:none"></g><g id="wpvSnow" style="display:none"></g></g>
    <rect class="soil1" x="0" y="390" width="1200" height="230"/>
    <rect class="soil2" x="0" y="468" width="1200" height="72"/>
    <rect class="soil3" x="0" y="540" width="1200" height="80"/>
    <g id="wpvPebbles"></g>
    <?php if ($scene === 'wasser'): ?>
    <rect class="gw" x="0" y="542" width="580" height="78"/>
    <g class="gwa">
      <path d="M60 575 l10 6 l-10 6"/><path d="M150 590 l10 6 l-10 6"/><path d="M330 570 l10 6 l-10 6"/><path d="M510 588 l10 6 l-10 6"/>
    </g>
    <?php endif; ?>
    <rect class="grass" x="0" y="382" width="1200" height="10" rx="3"/>
    <rect id="wpvSnowGround" x="0" y="378" width="580" height="7" rx="3" fill="#f4f8fb" style="display:none"/>

    <!-- Wärmequelle -->
    <?php if ($scene === 'sole'): ?>
    <g id="wpvSrcSonde">
      <rect class="bore" x="430" y="398" width="36" height="206" rx="6"/>
      <text class="lbl" x="448" y="614" text-anchor="middle">Erdsonde</text>
    </g>
    <?php elseif ($scene === 'kollektor'): ?>
    <g id="wpvSrcKollektor"><text class="lbl" x="330" y="508" text-anchor="middle"><?= $s['source'] === 'direct' ? 'Direktverdampfung' : 'Flächenkollektor' ?></text></g>
    <?php elseif ($scene === 'wasser'): ?>
    <g id="wpvSrcWasser">
      <rect class="bore" x="430" y="398" width="20" height="182" rx="2"/>
      <rect class="bore" x="240" y="398" width="20" height="182" rx="2"/>
      <rect x="434" y="556" width="12" height="18" rx="2" fill="#7c8794"/>
      <text class="lbl" x="440" y="604" text-anchor="middle">Förderbrunnen</text>
      <text class="lbl" x="250" y="604" text-anchor="middle">Schluckbrunnen</text>
    </g>
    <?php elseif ($scene === 'luft'): ?>
    <g id="wpvSrcLuft">
      <rect class="pad" x="392" y="382" width="162" height="8" rx="2"/>
      <rect class="unit" x="400" y="290" width="142" height="92" rx="7"/>
      <circle class="grille-ring" cx="452" cy="336" r="36"/>
      <g transform="translate(452 336)"><g class="blades">
        <circle r="31" fill="none"/>
        <path d="M0 0 C8 -8 16 -22 4 -30 C-6 -24 -6 -10 0 0Z"/>
        <path d="M0 0 C8 -8 16 -22 4 -30 C-6 -24 -6 -10 0 0Z" transform="rotate(90)"/>
        <path d="M0 0 C8 -8 16 -22 4 -30 C-6 -24 -6 -10 0 0Z" transform="rotate(180)"/>
        <path d="M0 0 C8 -8 16 -22 4 -30 C-6 -24 -6 -10 0 0Z" transform="rotate(270)"/>
      </g></g>
      <circle cx="452" cy="336" r="5" fill="#9aa4b1"/>
      <line class="vent" x1="500" y1="304" x2="532" y2="304"/><line class="vent" x1="500" y1="314" x2="532" y2="314"/><line class="vent" x1="500" y1="324" x2="532" y2="324"/><line class="vent" x1="500" y1="334" x2="532" y2="334"/><line class="vent" x1="500" y1="344" x2="532" y2="344"/><line class="vent" x1="500" y1="354" x2="532" y2="354"/><line class="vent" x1="500" y1="364" x2="532" y2="364"/>
      <text class="lbl" x="471" y="404" text-anchor="middle">Außeneinheit</text>
      <g class="air">
        <path d="M340 312 l12 6 l-12 6"/><path d="M334 332 l12 6 l-12 6"/><path d="M340 352 l12 6 l-12 6"/>
      </g>
      <?php if ($s['ice']): ?>
      <g class="ice">
        <path d="M418 300 v12 M412 306 h12 M414 302 l8 8 M422 302 l-8 8"/>
        <path d="M486 372 v10 M481 377 h10"/>
        <path d="M520 296 v10 M515 301 h10"/>
      </g>
      <?php endif; ?>
    </g>
    <?php else: ?>
    <g id="wpvSrcNeutral">
      <title>Wärmequelle in der Konfiguration nicht eingestellt</title>
      <rect class="pad" x="392" y="382" width="162" height="8" rx="2"/>
      <rect class="unit" x="400" y="290" width="142" height="92" rx="7"/>
      <text class="lbl" x="471" y="332" text-anchor="middle">Wärmequelle</text>
      <text class="lbl" x="471" y="350" text-anchor="middle">nicht eingestellt</text>
    </g>
    <?php endif; ?>

    <!-- Haus -->
    <g id="wpvHouse">
      <rect class="slab" x="580" y="386" width="605" height="30"/>
      <rect class="wall" x="580" y="150" width="605" height="238"/>
      <polygon class="roof" points="562,154 882,46 1202,154"/>
      <polygon id="wpvSnowRoof" points="566,150 882,40 1198,150 1188,152 882,48 576,152" fill="#f4f8fb" style="display:none"/>
      <rect class="room" x="598" y="168" width="569" height="212"/>
      <rect id="wpvWarmTint" x="598" y="168" width="569" height="212" fill="#ffcf73" opacity="0"/>
      <rect class="wall-inner" x="905" y="168" width="10" height="212"/>
      <rect class="screed" x="915" y="366" width="252" height="14"/>
      <rect class="win-frame" x="1046" y="198" width="104" height="62" rx="3"/>
      <rect x="1050" y="202" width="96" height="54" fill="url(#wpvSkyGrad)"/>
      <line x1="1098" y1="202" x2="1098" y2="256" stroke="var(--win-frame)" stroke-width="3"/>
      <circle id="wpvLampGlowC" cx="1038" cy="268" r="84" fill="url(#wpvLampGlow)" opacity="0"/>
      <line class="lamp-stand" x1="1038" y1="270" x2="1038" y2="366"/>
      <path class="lamp-shade" d="M1026 270 L1032 256 H1044 L1050 270Z"/>
      <path class="sofa2" d="M930 330 h92 v14 h-92z"/>
      <path class="sofa" d="M926 342 h100 v22 h-100z"/>
      <rect class="sofa2" x="920" y="336" width="10" height="28" rx="3"/><rect class="sofa2" x="1022" y="336" width="10" height="28" rx="3"/>
    </g>

    <!-- Rohrleitungen: Vorlauf rot, Rücklauf blau, Quelle türkis/blau -->
    <g id="wpvPipes"><?= $pipes ?></g>

    <!-- Geräte -->
    <g id="wpvEquipment">
      <g id="wpvUnit">
        <rect class="cabinet" x="612" y="236" width="70" height="144" rx="5"/>
        <rect class="display" x="620" y="248" width="54" height="20" rx="3"/>
        <text class="display-text" x="647" y="262" text-anchor="middle"<?= strlen($meta[0]) > 8 ? ' textLength="48" lengthAdjust="spacingAndGlyphs"' : '' ?>><?= wpv_h($meta[0]) ?></text>
        <circle cx="675" cy="243" r="3" fill="<?= wpv_h($meta[2]) ?>"/>
        <text class="cab-label" x="647" y="290" text-anchor="middle"><?= $scene === 'luft' ? 'Hydraulik' : 'Wärmepumpe' ?></text>
        <line class="grille-line" x1="622" y1="330" x2="672" y2="330"/><line class="grille-line" x1="622" y1="338" x2="672" y2="338"/><line class="grille-line" x1="622" y1="346" x2="672" y2="346"/><line class="grille-line" x1="622" y1="354" x2="672" y2="354"/>
        <?php if ($s['mode'] === 'evu'): ?>
        <g transform="translate(647 312)">
          <rect x="-9" y="-4" width="18" height="14" rx="2" fill="var(--purple)"/>
          <path d="M-5 -4 v-4 a5 5 0 0 1 10 0 v4" fill="none" stroke="var(--purple)" stroke-width="2.4"/>
        </g>
        <?php endif; ?>
      </g>
      <g id="wpvValve" class="valve">
        <title>Umschaltventil: Stellung aus der Betriebsart abgeleitet, nicht gemessen</title>
        <polygon points="710,294 710,306 722,300"<?= $flags['wp'] ? ' class="on"' : '' ?>/>
        <polygon points="734,294 734,306 722,300"<?= $flags['ww'] ? ' class="on"' : '' ?>/>
        <polygon points="716,288 728,288 722,300"<?= $flags['hz'] ? ' class="on"' : '' ?>/>
      </g>
      <g id="wpvWwTank">
        <rect class="tank" x="748" y="250" width="60" height="130" rx="14" fill="url(#wpvGradWW)"/>
        <text class="tank-lbl" x="778" y="266" text-anchor="middle">Warmwasser</text>
        <?= wpv_svg_text('class="tank-val" x="778" y="288" text-anchor="middle"', $f1($ww, ' °C'), $ww['why']) ?>
        <?= wpv_svg_text('class="tank-lbl" x="778" y="374" text-anchor="middle"', 'Soll ' . ($wwSoll['v'] === null ? '--' : wpv_fmt($wwSoll['v'], 0, ' °C')), $wwSoll['why']) ?>
      </g>
      <g id="wpvCoilLayer"><?= $coil ?></g>
      <?php if ($buffer['present']): ?>
      <g id="wpvBufTank">
        <title><?= wpv_h('Pufferspeicher · ' . $buffer['label'] . ($buffer['why'] !== '' ? ' · ' . $buffer['why'] : '')) ?></title>
        <rect class="tank" x="832" y="262" width="50" height="118" rx="14" fill="url(#wpvGradBuf)"/>
        <text class="tank-lbl" x="857" y="280" text-anchor="middle">Puffer</text>
        <?= wpv_svg_text('class="tank-val" x="857" y="302" text-anchor="middle" style="font-size:12px"', $f1($buffer, ' °C'), $buffer['why']) ?>
      </g>
      <?php endif; ?>
      <?php if ($s['circuit'] === 'hk'): ?>
      <g class="radiator">
        <rect x="1052" y="286" width="92" height="48" rx="4"/>
        <rect x="1060" y="290" width="6" height="40" rx="2"/><rect x="1072" y="290" width="6" height="40" rx="2"/><rect x="1084" y="290" width="6" height="40" rx="2"/><rect x="1096" y="290" width="6" height="40" rx="2"/><rect x="1108" y="290" width="6" height="40" rx="2"/><rect x="1120" y="290" width="6" height="40" rx="2"/><rect x="1132" y="290" width="6" height="40" rx="2"/>
      </g>
      <g class="heat">
        <path d="M1070 280 q5 -6 0 -12 q-5 -6 0 -12"/><path d="M1098 282 q5 -6 0 -12 q-5 -6 0 -12" style="animation-delay:.8s"/><path d="M1126 280 q5 -6 0 -12 q-5 -6 0 -12" style="animation-delay:1.5s"/>
      </g>
      <?php else: ?>
      <g class="heat">
        <path d="M950 356 q5 -6 0 -12 q-5 -6 0 -12"/><path d="M1000 358 q5 -6 0 -12 q-5 -6 0 -12" style="animation-delay:.9s"/><path d="M1070 356 q5 -6 0 -12 q-5 -6 0 -12" style="animation-delay:1.6s"/><path d="M1130 358 q5 -6 0 -12 q-5 -6 0 -12" style="animation-delay:.4s"/>
      </g>
      <?php endif; ?>
    </g>

    <!-- Wertekärtchen -->
    <g id="wpvChips">
      <g class="chip" transform="translate(16 16)">
        <rect class="bg" width="262" height="58" rx="12"/>
        <g id="wpvWxIcon" transform="translate(30 29)"></g>
        <text class="c-lbl" x="58" y="22">Außen</text>
        <?= wpv_svg_text('class="c-val" x="58" y="45"', $f1($v['aussen'], ' °C'), $v['aussen']['why']) ?>
        <?= wpv_svg_text('class="c-sub" x="146" y="45"', 'Ø ' . $f1($v['mittel'], ' °C'), $v['mittel']['why']) ?>
        <?php if ($s['season'] !== null): ?>
        <g class="season<?= $s['season'] === 'sommer' ? ' sommer' : '' ?>" transform="translate(204 12)"><title><?= wpv_h('Gemittelte Außentemperatur gegen die Heizgrenze ' . wpv_fmt($s['heizgrenze'], 1, ' °C')) ?></title><rect width="48" height="18" rx="9"/><text x="24" y="13" text-anchor="middle"><?= $s['season'] === 'sommer' ? 'Sommer' : 'Winter' ?></text></g>
        <?php endif; ?>
      </g>
      <g class="chip opt" transform="translate(984 16)">
        <rect class="bg" width="200" height="58" rx="12"/>
        <text class="c-lbl" x="12" y="22">Tageszeit</text>
        <text id="wpvZeit" class="c-val" x="12" y="45">--:--</text>
        <text id="wpvZeitSub" class="c-sub" x="76" y="45">--</text>
      </g>
      <g class="chip" transform="<?= $luftLike ? 'translate(214 222)' : 'translate(24 500)' ?>">
        <rect class="bg" width="214" height="58" rx="12"/>
        <text class="c-lbl" x="12" y="22"><?= wpv_h($s['source_chip']) ?></text>
        <?= wpv_svg_text('class="c-val sm" x="12" y="42"', $srcText, $srcWhy) ?>
        <text class="c-sub" x="12" y="54"><?= wpv_h($srcSub) ?></text>
      </g>
      <g class="chip" transform="translate(606 174)">
        <rect class="bg" width="148" height="58" rx="12"/>
        <text class="c-lbl" x="12" y="20">Heizleistung<?= $s['heiz_estimated'] ? ' (geschätzt)' : '' ?></text>
        <?= wpv_svg_text('class="c-val" x="12" y="40"', $heizText, $v['heiz_kw']['why']) ?>
        <?= wpv_svg_text('class="c-sub" x="12" y="53"', $wpSub, trim($v['elec_kw']['why'] . ' ' . $v['cop']['why'])) ?>
      </g>
      <g class="chip opt" transform="translate(762 174)">
        <rect class="bg" width="136" height="58" rx="12"/>
        <text class="c-lbl" x="12" y="20">Vorlauf / Rücklauf</text>
        <?= wpv_svg_text('class="c-val sm" x="12" y="39"', $vlText, trim($v['vl']['why'] . ' ' . $v['rl']['why'])) ?>
        <?= wpv_svg_text('class="c-sub" x="12" y="53"', $vlSub, $vlSubWhy) ?>
      </g>
      <g class="chip opt" transform="translate(925 176)">
        <rect class="bg" width="118" height="44" rx="12"/>
        <text class="c-lbl" x="12" y="19">Heizkreis</text>
        <?= wpv_svg_text('class="c-val sm" x="12" y="36" style="font-size:12.5px"', $s['hk_text'], $s['hk_why']) ?>
      </g>
    </g>
  </svg>
  </div>
  <div class="legend">
    <span><i style="background:var(--hot)"></i>Vorlauf</span>
    <span><i style="background:var(--cold)"></i>Rücklauf</span>
    <span><i style="background:var(--src-warm)"></i>Quelle zur WP</span>
    <span><i style="background:var(--src-cold)"></i>Quelle von der WP</span>
    <span>Laufende Punkte = Pumpe fördert (nur wenn der Messwert das belegt)</span>
  </div>

  <div class="statusbar">
    <span class="wpv-badge <?= wpv_h($meta[1]) ?>" title="<?= wpv_h($s['status_title']) ?>"><?= wpv_h($s['status_text']) ?></span>
    <span class="pill">Verdichter <b<?= $v['hz_ist']['why'] !== '' ? ' title="' . wpv_h($v['hz_ist']['why']) . '"' : '' ?>><?= wpv_h($hzIst) ?></b> · Soll <b<?= $v['hz_soll']['why'] !== '' ? ' title="' . wpv_h($v['hz_soll']['why']) . '"' : '' ?>><?= wpv_h($hzSoll) ?></b></span>
    <?php if ($s['ww_window'] !== null): ?><span class="pill" title="<?= $s['ww_window']['active'] ? 'gerade im Fenster' : 'gerade außerhalb' ?>">Warmwasser-Fenster <?= wpv_h($s['ww_window']['text']) ?></span><?php endif; ?>
    <?php if ($s['circ_window'] !== null): ?><span class="pill" title="Zeitfenster der Zirkulation (Konfiguration)">Zirkulation <?= wpv_h($s['circ_window']['text']) ?></span><?php endif; ?>
    <?php foreach ($s['extra_badges'] as [$cls, $text, $title]): ?><span class="wpv-badge <?= wpv_h($cls) ?>" title="<?= wpv_h($title) ?>"><?= wpv_h($text) ?></span><?php endforeach; ?>
  </div>

  <div class="tabs" role="tablist">
    <button class="tab" type="button" role="tab" aria-selected="true">Übersicht</button>
  </div>
  <div class="panel">
    <section role="tabpanel" aria-label="Übersicht">
      <div class="grid">
        <?= wpv_tile('Außen (Ist / Mittel)', wpv_span($v['aussen'], 1, ' °C') . ' <span class="s">/ ' . wpv_span($v['mittel'], 1, ' °C') . '</span>') ?>
        <?= wpv_tile('Vorlauf' . ($s['wp_type'] === 1 ? ' (Ist / Soll)' : ''), wpv_span($v['vl'], 1, ' °C', 't-hot') . ($s['wp_type'] === 1 ? ' <span class="s">/ ' . wpv_span($v['vl_soll'], 1, ' °C') . '</span>' : '')) ?>
        <?= wpv_tile($s['wp_type'] === 1 ? 'Rücklauf' : ($s['wp_type'] === 4 ? 'Rücklauf (Ist / HK-Soll)' : 'Rücklauf (Ist / Soll)'), wpv_span($v['rl'], 1, ' °C', 't-cold') . ($s['wp_type'] === 1 ? '' : ' <span class="s">/ ' . wpv_span($v['rl_soll'], 1, ' °C') . '</span>')) ?>
        <?= wpv_tile('Warmwasser (Ist / Soll)', wpv_span($ww, 1, ' °C', 't-ww') . ' <span class="s">/ ' . wpv_span($wwSoll, 0, ' °C') . '</span>', $wwNote !== '' ? '<div class="s">' . wpv_h($wwNote) . '</div>' : '') ?>
        <?php if ($s['source_single']): ?>
        <?= wpv_tile(wpv_h($s['source_chip']), wpv_span($v['src_ein'], 1, ' °C', 't-src')) ?>
        <?php else: ?>
        <?= wpv_tile(($s['source'] === 'air' ? 'Außenluft' : 'Wärmequelle') . ' (ein / aus)', '<span class="t-src">' . wpv_span($v['src_ein'], 1, '') . ' / ' . wpv_span($v['src_aus'], 1, ' °C') . '</span>') ?>
        <?php endif; ?>
        <?php if ($buffer['present']): ?>
        <?= wpv_tile('Pufferspeicher', wpv_span($buffer, 1, ' °C'), '<div class="s">' . wpv_h($buffer['label']) . '</div>', ' id="wpvBufTile"') ?>
        <?php endif; ?>
        <?= wpv_tile('Heizleistung' . ($s['heiz_estimated'] ? ' (geschätzt)' : ''), wpv_span($v['heiz_kw'], 1, ' kW')) ?>
        <?= wpv_tile('Verbrauch', wpv_span($v['elec_kw'], 2, ' kW', 't-ww')) ?>
        <?= wpv_tile('COP (jetzt)', wpv_span($v['cop'], 2, '', $s['cop_class'])) ?>
        <?= wpv_tile(wpv_h($s['taz_label']) . ' / JAZ', wpv_span($v['taz'], 2, '', $s['taz_class']) . ' <span class="s">/ ' . wpv_span($v['jaz'], 2, '', $s['jaz_class']) . '</span>', $s['taz_note'] !== '' ? '<div class="s">' . wpv_h($s['taz_note']) . '</div>' : '') ?>
      </div>
      <p class="note">Fehlt ein Messwert, steht dort „--“ mit dem Grund im Tooltip; es wird nie ein Ersatzwert wie 0 angezeigt.</p>
    </section>
  </div>
  <div class="foot">
    <span>Datenstand: <?= wpv_h($s['data_time'] ?? '--') ?></span>
    <span>Daten von <?= wpv_h($s['manufacturer']) ?> · Vorschau, nur Anzeige</span>
  </div>
</div>
</div>
<script>
(function () {
  'use strict';
  if (window.__wpvBooted) return;
  window.__wpvBooted = true;
  const NS = 'http://www.w3.org/2000/svg';
  const $ = (id) => document.getElementById(id);
  const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
  const hex2rgb = (h) => { h = h.replace('#', ''); return [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16)); };
  const rgb2hex = (a) => '#' + a.map(v => Math.round(clamp(v, 0, 255)).toString(16).padStart(2, '0')).join('');
  const mix = (a, b, t) => { const A = hex2rgb(a), B = hex2rgb(b); return rgb2hex(A.map((v, i) => v + (B[i] - v) * t)); };
  const ELEV = [-18, -12, -6, -2, 2, 8, 20, 45];
  function lerpKeys(vals, x) {
    if (x <= ELEV[0]) return vals[0];
    for (let i = 1; i < ELEV.length; i++) {
      if (x <= ELEV[i]) {
        const t = (x - ELEV[i - 1]) / (ELEV[i] - ELEV[i - 1]);
        return typeof vals[0] === 'number' ? vals[i - 1] + (vals[i] - vals[i - 1]) * t : mix(vals[i - 1], vals[i], t);
      }
    }
    return vals[vals.length - 1];
  }
  const PAL = {
    dark: {
      skyTop: ['#03060d', '#060b1a', '#141c44', '#262d66', '#39579a', '#3372b8', '#2c73bb', '#2a70b8'],
      skyMid: ['#060a17', '#0c1430', '#28295c', '#5f4677', '#8a7ba6', '#6aa0d2', '#5c98d1', '#5896d1'],
      skyBot: ['#0a1124', '#131c40', '#4a3b69', '#cf6f55', '#eaa672', '#b9dcf0', '#a6d3ef', '#a1d0ef'],
      hillFar: ['#0f181c', '#121d22', '#1b2830', '#2b3739', '#3d5746', '#477652', '#4b7d55', '#4b7d55'],
      hillNear: ['#0b1316', '#0e171b', '#152026', '#232f28', '#324d3a', '#3a6845', '#3d6e47', '#3d6e47'],
      grass: ['#0d1812', '#101e16', '#17271c', '#253628', '#34532e', '#427634', '#488539', '#488539'],
      wall: ['#2a2f38', '#2e333e', '#383d4a', '#565760', '#9a9ea6', '#bfc4cb', '#cacfd5', '#cacfd5'],
      roof: ['#1c1513', '#201816', '#2a1e1b', '#462b24', '#66382d', '#783f30', '#7d4233', '#7d4233'],
      lamp: [1, 1, .9, .6, .25, 0, 0, 0],
      stars: [1, .85, .4, .06, 0, 0, 0, 0],
      // Innenraum folgt dem Tageslicht, nicht dem UI-Modus: tagsüber helle Wände auch im Dunkelmodus
      room: ['#1e232b', '#20252d', '#272c34', '#3b3e45', '#b8bdc5', '#e2e5e9', '#eceef1', '#eceef1'],
      wallInner: ['#3a414b', '#3c434d', '#434a54', '#595e66', '#aeb4bc', '#c9ced5', '#d2d6dc', '#d2d6dc'],
      screed: ['#3f3b36', '#413d38', '#47423c', '#5a544c', '#b3aa9c', '#d2c9bb', '#d9d0c3', '#d9d0c3'],
      cloudNight: '#2c3342', cloudDay: '#dde4ec', veil: '#2a303a', storm: '#5a6270'
    },
    light: {
      skyTop: ['#6878b2', '#6f80ba', '#8589c4', '#b596bd', '#8cb1e0', '#6cb0e9', '#58a8e9', '#52a4e8'],
      skyMid: ['#8794c6', '#909ccb', '#ada4d1', '#dfb0b7', '#c4d4ee', '#a7d2f2', '#9accf2', '#95caf2'],
      skyBot: ['#aab5dd', '#b2bce0', '#d8bdcf', '#fdcaa3', '#fadfbd', '#dceffb', '#d4ebfb', '#d0e9fb'],
      hillFar: ['#7e90ab', '#8395af', '#8c9cb1', '#a0a79a', '#8cae89', '#84b680', '#80b77d', '#80b77d'],
      hillNear: ['#6c819f', '#7186a3', '#7b8da5', '#919884', '#739e6e', '#67a662', '#63a85e', '#63a85e'],
      grass: ['#778ca3', '#7c91a7', '#8898a6', '#98a580', '#7dae67', '#6db155', '#67b24f', '#67b24f'],
      wall: ['#dde1ec', '#e0e4ee', '#e6e7ef', '#f1eae4', '#f5f2ee', '#f7f7f5', '#f9f9f7', '#f9f9f7'],
      roof: ['#8a6977', '#8e6b78', '#996e74', '#ae6f5e', '#b2634e', '#af5d47', '#ae5a44', '#ae5a44'],
      lamp: [.85, .85, .7, .45, .2, 0, 0, 0],
      stars: [.85, .65, .28, .05, 0, 0, 0, 0],
      room: ['#e4dfe9', '#e6e1eb', '#ebe5ea', '#f2ece6', '#f7f4ef', '#fbfaf7', '#fbfaf7', '#fbfaf7'],
      wallInner: ['#c6c9d6', '#c8cbd8', '#cdcfd9', '#d6d6d9', '#d9dde3', '#d9dde3', '#d9dde3', '#d9dde3'],
      screed: ['#cfc9d2', '#d1cbd3', '#d6cfd1', '#dcd4ca', '#ddd6cb', '#ddd6cb', '#ddd6cb', '#ddd6cb'],
      cloudNight: '#c0c6de', cloudDay: '#ffffff', veil: '#9aa3b0', storm: '#7d8794'
    }
  };

  // Sonne und Mond (Formeln nach SunCalc)
  function sunPosition(date, lat, lon) {
    const rad = Math.PI / 180, dayMs = 86400000, J1970 = 2440588, J2000 = 2451545;
    const d = date.valueOf() / dayMs - 0.5 + J1970 - J2000;
    const M = rad * (357.5291 + 0.98560028 * d);
    const C = rad * (1.9148 * Math.sin(M) + 0.02 * Math.sin(2 * M) + 0.0003 * Math.sin(3 * M));
    const L = M + C + rad * 102.9372 + Math.PI;
    const e = rad * 23.4397;
    const dec = Math.asin(Math.sin(e) * Math.sin(L));
    const ra = Math.atan2(Math.sin(L) * Math.cos(e), Math.cos(L));
    const lw = rad * -lon, phi = rad * lat;
    const H = rad * (280.16 + 360.9856235 * d) - lw - ra;
    const alt = Math.asin(Math.sin(phi) * Math.sin(dec) + Math.cos(phi) * Math.cos(dec) * Math.cos(H));
    const az = Math.atan2(Math.sin(H), Math.cos(H) * Math.sin(phi) - Math.tan(dec) * Math.cos(phi));
    return { alt: alt / rad, az: az / rad + 180 };
  }
  function moonAge(date) {
    const jd = date.valueOf() / 86400000 + 2440587.5;
    const p = ((jd - 2451550.26) / 29.530588853) % 1;
    return p < 0 ? p + 1 : p;
  }
  function moonPath(r, p) {
    const k = Math.cos(2 * Math.PI * p), rx = Math.abs(k) * r, waxing = p < 0.5;
    const outer = waxing ? 1 : 0, inner = ((k > 0) === waxing) ? 0 : 1;
    return `M0,${-r} A${r},${r} 0 0 ${outer} 0,${r} A${rx},${r} 0 0 ${inner} 0,${-r} Z`;
  }
  function rng(seed) { return () => { seed |= 0; seed = seed + 0x6D2B79F5 | 0; let t = Math.imul(seed ^ seed >>> 15, 1 | seed); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }
  function el(name, attrs, parent) { const n = document.createElementNS(NS, name); for (const k in attrs) n.setAttribute(k, attrs[k]); if (parent) parent.appendChild(n); return n; }
  const show = (n, on) => { if (n) n.style.display = on ? '' : 'none'; };

  let S = null;
  let loadedAt = Date.now();
  const CLOUDS = [[40, 70, 1.0], [300, 36, 1.3], [520, 96, 0.9], [760, 26, 1.1], [980, 84, 1.2], [1110, 30, 0.8]];

  // Dekoration mit festem Startwert: jedes Laden zeichnet dieselben Sterne, Steine und Wolken.
  function decorate() {
    const R = rng(7);
    const stars = $('wpvStars'), pebbles = $('wpvPebbles'), clouds = $('wpvClouds'), rain = $('wpvRain'), snow = $('wpvSnow');
    if (!stars || stars.childNodes.length) return;
    for (let i = 0; i < 55; i++) el('circle', { cx: (R() * 1200).toFixed(1), cy: (R() * 290).toFixed(1), r: (0.6 + R() * 1.3).toFixed(2), fill: '#fff', opacity: (0.4 + R() * 0.6).toFixed(2) }, stars);
    for (let i = 0; i < 70; i++) el('ellipse', { class: 'pebble', cx: (R() * 1200).toFixed(1), cy: (400 + R() * 215).toFixed(1), rx: (1.5 + R() * 3).toFixed(1), ry: (1 + R() * 2).toFixed(1) }, pebbles);
    CLOUDS.forEach(([x, y, s], i) => {
      const w = el('g', { class: 'cloud', 'data-i': i, opacity: 0 }, clouds);
      const d = el('g', { class: 'drift', style: `animation-duration:${70 + i * 13}s;animation-delay:${-i * 11}s` }, w);
      el('use', { href: '#wpvCloudShape', x: x, y: y, width: 140 * s, height: 70 * s }, d);
    });
    for (let i = 0; i < 80; i++) { const x = R() * 1200, y = R() * 380; el('line', { class: 'drop', x1: x.toFixed(1), y1: y.toFixed(1), x2: (x - 4).toFixed(1), y2: (y + 14).toFixed(1), style: `animation-delay:${(-R()).toFixed(2)}s` }, rain); }
    for (let i = 0; i < 80; i++) { el('circle', { class: 'flake', cx: (R() * 1200).toFixed(1), cy: (R() * 380).toFixed(1), r: (1.2 + R() * 1.8).toFixed(1), style: `animation-delay:${(-R() * 7).toFixed(2)}s` }, snow); }
  }

  function effectiveTheme() {
    const h = document.documentElement;
    const b = document.body;
    // index.php setzt das Thema am html-Element, mobile.php beim Laden nur am body
    const t = h.getAttribute('data-bs-theme') || h.getAttribute('data-theme')
      || (b && (b.getAttribute('data-bs-theme') || b.getAttribute('data-theme'))) || '';
    return t === 'dark' ? 'dark' : 'light';
  }
  function sceneDate() { return (S && S.timeOverrideMs) ? new Date(S.timeOverrideMs) : new Date(); }
  const WX_TEXT = { klar: 'klar', leicht: 'leicht bewölkt', bedeckt: 'bedeckt', regen: 'Regen', schnee: 'Schnee', none: 'Wetter --' };

  function weatherIcon(wx, night) {
    const g = $('wpvWxIcon'); if (!g) return;
    g.textContent = '';
    const cloud = (x, y, s, fill) => el('use', { href: '#wpvCloudShape', x, y, width: 140 * s, height: 70 * s, fill }, g);
    if (wx === 'none') {
      // Ohne Wetterdaten nur ein neutrales Thermometer, keine erfundene Wetterlage
      el('rect', { x: -3, y: -12, width: 6, height: 17, rx: 3, fill: 'none', stroke: 'var(--chip-muted)', 'stroke-width': 1.6 }, g);
      el('circle', { cx: 0, cy: 8, r: 5, fill: 'var(--chip-muted)' }, g);
      return;
    }
    if (wx === 'klar' || wx === 'leicht') {
      if (night) { el('path', { d: moonPath(10, 0.3), fill: '#f3efe2', stroke: '#c9c3ad' }, g); }
      else { el('circle', { r: 8, fill: '#ffc83d' }, g); for (let i = 0; i < 8; i++) { const a = i * Math.PI / 4; el('line', { x1: Math.cos(a) * 11, y1: Math.sin(a) * 11, x2: Math.cos(a) * 15, y2: Math.sin(a) * 15, stroke: '#ffc83d', 'stroke-width': 2, 'stroke-linecap': 'round' }, g); } }
      if (wx === 'leicht') cloud(-12, -2, 0.2, '#cfd8e3');
    } else {
      cloud(-15, -12, 0.22, wx === 'bedeckt' ? '#aab4c0' : '#98a3b0');
      if (wx === 'regen') for (let i = 0; i < 3; i++) el('line', { x1: -6 + i * 7, y1: 6, x2: -9 + i * 7, y2: 13, stroke: '#4da3ff', 'stroke-width': 2, 'stroke-linecap': 'round' }, g);
      if (wx === 'schnee') for (let i = 0; i < 3; i++) el('circle', { cx: -6 + i * 7, cy: 10, r: 1.8, fill: '#9fd3ff' }, g);
    }
  }

  function applyScene() {
    const svg = $('wpvScene'); if (!svg || !S) return;
    const date = sceneDate();
    const { alt, az } = sunPosition(date, S.lat, S.lon);
    const th = effectiveTheme();
    const P = PAL[th];
    const wx = S.wx || 'none';
    const over = wx === 'bedeckt' || wx === 'regen' || wx === 'schnee';
    const desat = over ? 0.45 : (wx === 'leicht' ? 0.12 : 0);
    const day = clamp((alt + 6) / 14, 0, 1);
    const set = (k, v) => svg.style.setProperty(k, v);
    set('--sky-top', mix(lerpKeys(P.skyTop, alt), P.veil, desat));
    set('--sky-mid', mix(lerpKeys(P.skyMid, alt), P.veil, desat));
    set('--sky-bot', mix(lerpKeys(P.skyBot, alt), P.veil, desat * 0.8));
    set('--hill-far', lerpKeys(P.hillFar, alt));
    set('--hill-near', lerpKeys(P.hillNear, alt));
    let grass = lerpKeys(P.grass, alt);
    if (typeof S.aussen === 'number' && S.aussen < 0) grass = mix(grass, '#dfeaf2', clamp(-S.aussen / 8, 0.25, 0.6));
    if (wx === 'schnee') grass = mix(grass, '#eef4f8', 0.8);
    set('--grass', grass);
    set('--wall', lerpKeys(P.wall, alt));
    set('--roof', lerpKeys(P.roof, alt));
    set('--room', lerpKeys(P.room, alt));
    set('--wall-inner', lerpKeys(P.wallInner, alt));
    set('--screed', lerpKeys(P.screed, alt));
    set('--veil', P.veil);
    let cloud = mix(P.cloudNight, P.cloudDay, day);
    if (over) cloud = mix(cloud, P.storm, 0.35 * day);
    set('--cloud', cloud);
    $('wpvVeil').setAttribute('opacity', over ? (wx === 'regen' ? 0.4 : wx === 'schnee' ? 0.22 : 0.3) : 0);
    const starOp = lerpKeys(P.stars, alt) * (over ? 0.08 : (wx === 'leicht' ? 0.6 : 1));
    $('wpvStars').setAttribute('opacity', starOp.toFixed(2));
    // Sonnenbahn links vom Haus: mittags hoch über dem Garten, abends hinter dem Dach
    const sx = clamp(60 + (az - 90) / 180 * 900, 20, 1180), sy = clamp(300 - alt * 5.2, 36, 380);
    const sun = $('wpvSun');
    sun.setAttribute('transform', `translate(${sx.toFixed(1)} ${sy.toFixed(1)})`);
    sun.setAttribute('opacity', (clamp((alt + 3) / 5, 0, 1) * (over ? 0.3 : wx === 'leicht' ? 0.85 : 1)).toFixed(2));
    $('wpvSunDisk').setAttribute('fill', mix('#ff9a4d', '#ffe27a', clamp(alt / 12, 0, 1)));
    const maz = (az + 180) % 360, malt = clamp(-alt * 0.9, 8, 55);
    const mx = clamp(60 + (maz - 90) / 180 * 900, 40, 1140), my = clamp(300 - malt * 5.2, 44, 300);
    const moon = $('wpvMoon');
    moon.setAttribute('transform', `translate(${mx.toFixed(1)} ${my.toFixed(1)})`);
    moon.setAttribute('opacity', (clamp((-alt - 2) / 6, 0, 1) * (over ? 0.25 : 1)).toFixed(2));
    $('wpvMoonLit').setAttribute('d', moonPath(16, moonAge(date)));
    // Wolken nur aus dem Wettercode; ohne Wetterdaten keine Wolken
    const sets = { klar: [1], leicht: [0, 2, 4], bedeckt: [0, 1, 2, 3, 4, 5], regen: [0, 1, 2, 3, 4, 5], schnee: [0, 1, 2, 3, 4, 5], none: [] };
    document.querySelectorAll('#wpvClouds .cloud').forEach(c => {
      const on = (sets[wx] || []).includes(+c.dataset.i);
      c.setAttribute('opacity', on ? (wx === 'klar' ? 0.45 : wx === 'leicht' ? 0.85 : 0.95) : 0);
    });
    show($('wpvRain'), wx === 'regen'); show($('wpvSnow'), wx === 'schnee');
    show($('wpvSnowGround'), wx === 'schnee'); show($('wpvSnowRoof'), wx === 'schnee');
    const lamp = lerpKeys(P.lamp, alt);
    $('wpvLampGlowC').setAttribute('opacity', lamp.toFixed(2));
    $('wpvWarmTint').setAttribute('opacity', (lamp * (th === 'dark' ? 0.08 : 0.05)).toFixed(3));
    const hh = String(date.getHours()).padStart(2, '0'), mm = String(date.getMinutes()).padStart(2, '0');
    $('wpvZeit').textContent = `${hh}:${mm}`;
    const phase = alt > 6 ? 'Tag' : alt > -6 ? (az < 180 ? 'Morgen' : 'Abend') : 'Nacht';
    const sub = $('wpvZeitSub');
    sub.textContent = `${phase} · ${WX_TEXT[wx] || WX_TEXT.none}`;
    const why = [S.wxWhy, S.locNote].filter(Boolean).join(' · ');
    if (why) { const t = document.createElementNS(NS, 'title'); t.textContent = why; sub.appendChild(t); }
    weatherIcon(wx, alt < -2);
  }

  function tickAge() {
    const n = $('wpvAge'); if (!n || !S || typeof S.ageS !== 'number') return;
    n.textContent = 'Daten vor ' + Math.max(0, Math.round(S.ageS + (Date.now() - loadedAt) / 1000)) + ' s';
  }

  function init() {
    const node = $('wpv-state');
    try { S = node ? JSON.parse(node.textContent) : null; } catch (e) { S = null; }
    if (!S) return;
    loadedAt = Date.now();
    decorate();
    applyScene();
    tickAge();
  }

  // Aktualisierung: dieselbe Seite lesend nachladen (anmeldepflichtig, ohne Seiteneffekte).
  let busy = false;
  function refresh() {
    if (busy || document.hidden || (S && S.timeOverrideMs)) return;
    busy = true;
    const url = new URL(<?= json_encode($ajaxUrl, JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT | JSON_UNESCAPED_SLASHES) ?>, location.href);
    fetch(url.toString(), { credentials: 'same-origin', cache: 'no-store' })
      .then(r => (r.ok ? r.text() : ''))
      .then(text => {
        if (!text) return;
        const fresh = new DOMParser().parseFromString(text, 'text/html').getElementById('wpv-card');
        const cur = $('wpv-card');
        if (fresh && cur) { cur.replaceWith(document.importNode(fresh, true)); init(); }
      })
      .catch(() => {})
      .finally(() => { busy = false; });
  }

  init();
  // Rückmeldung nach einem Knopf nur kurz zeigen; die Aktualisierung ersetzt nur die Karte darunter
  setTimeout(() => { const f = $('wpvFlash'); if (f) f.remove(); }, 20000);
  setInterval(tickAge, 1000);
  setInterval(applyScene, 60000);
  setInterval(refresh, 10000);
  document.addEventListener('visibilitychange', () => {
    const root = $('wpv-root');
    if (root) root.classList.toggle('paused', document.hidden);
    if (!document.hidden) refresh();
  });
  new MutationObserver(applyScene).observe(document.documentElement, { attributes: true, attributeFilter: ['data-bs-theme', 'data-theme'] });
})();
</script>
<?php
    return (string)ob_get_clean();
}

} // Ende function_exists('wpv_render')

if (!defined('WPV_LIBRARY_ONLY')) {
    if (!function_exists('requireWebAuth')) {
        require_once __DIR__ . '/helpers.php';
    }
    requireWebAuth(false);
    $wpvConfResult = loadE3dcConfig();
    $wpvConf = (empty($wpvConfResult['error']) && is_array($wpvConfResult['config'] ?? null)) ? $wpvConfResult['config'] : [];
    $wpvAllowed = wpv_page_allowed($wpvConf);
    if (!$wpvAllowed['ok']) {
        echo '<div class="alert alert-info">' . wpv_h($wpvAllowed['why'])
            . ' <a href="' . wpv_h(e3dcWpViewEntrypoint()) . '?seite=waermepumpe&amp;wp_view=alt" class="alert-link">Zur bisherigen Ansicht</a></div>';
    } else {
        $wpvState = wpv_build_state($wpvConf, wpv_collect_inputs($wpvConf));
        $wpvState['csrf_input'] = e3dcCsrfInput();
        // Einmalige Rückmeldung nach einem Knopf; die Route in index.php/mobile.php holt sie (nicht der Ajax-Abruf)
        $wpvState['flash'] = (isset($wpvFlash) && is_string($wpvFlash)) ? $wpvFlash : null;
        echo wpv_render($wpvState);
    }
}
