<?php
/** Gemeinsame Saisonanzeige aus der Regelung oder denselben Temperaturquellen. */
function e3dcWpSeasonNumber($value): ?float
{
    if (is_bool($value) || $value === null || $value === '' || !is_numeric($value)) return null;
    $number = (float)$value;
    return is_finite($number) ? $number : null;
}

function e3dcWpSeasonDecision(string $path = '/var/www/html/ramdisk/energy_decision_latest.json'): array
{
    $record = is_file($path) ? json_decode((string)@file_get_contents($path), true) : null;
    return is_array($record) ? $record : [];
}

function e3dcWpSeason(array $data, array $conf, array $record = [], ?int $now = null): array
{
    $now = $now ?? time();
    $pick = static function (array $keys) use ($data): ?float {
        foreach ($keys as $key) {
            $value = e3dcWpSeasonNumber($data[$key] ?? null);
            if ($value !== null) return $value;
        }
        return null;
    };
    $mean = $pick(['Aussentemp_Mittel', 'Außentemperatur_Mittel', 'Gemittelte Außentemperatur',
        'Mitteltemperatur', 'Aussentemperatur_Mittel', 'Gemittelte Aussentemperatur', 'Aussen_Mittel']);
    $temperature = $mean ?? $pick(['Aussentemp', 'Außentemperatur', 'Aussentemperatur', 'Zuluft']);
    $limit = e3dcWpSeasonNumber($conf['heizgrenze_temp'] ?? null);
    $source = 'anzeige';
    $temperatureSource = $mean !== null ? 'mittel' : 'momentan';
    $season = ($temperature !== null && $limit !== null) ? ($temperature > $limit ? 'summer' : 'winter') : '';
    $hp = is_array($record['heatpump'] ?? null) ? $record['heatpump'] : [];
    $ts = e3dcWpSeasonNumber($record['ts'] ?? null);
    $publishedMean = e3dcWpSeasonNumber($hp['season_at_mittel_c'] ?? null);
    $publishedLimit = e3dcWpSeasonNumber($hp['season_heating_limit_c'] ?? null);
    if (($hp['season_valid'] ?? null) === true
        && $ts !== null && $ts > 0 && $ts <= $now && $now - $ts <= 900
        && isset($hp['type']) && (int)$hp['type'] === (int)($conf['wp_type'] ?? -1)
        && in_array($hp['season'] ?? '', ['winter', 'summer'], true)
        && $publishedMean !== null && $publishedLimit !== null) {
        $season = $hp['season'];
        $temperature = $publishedMean;
        $limit = $publishedLimit;
        $source = 'regelung';
        $temperatureSource = ($hp['season_temperature_source'] ?? 'mittel') === 'momentan' ? 'momentan' : 'mittel';
    }
    $hint = '';
    if ($season !== '') {
        $limitText = rtrim(rtrim(number_format($limit, 1, ',', ''), '0'), ',');
        $hint = ($temperatureSource === 'mittel' ? 'gemittelt ' : 'Außen ')
            . number_format($temperature, 1, ',', '') . ' °C, Heizgrenze ' . $limitText . ' °C';
    }
    return [
        'wp_season' => $season,
        'wp_season_label' => $season === '' ? '' : ($season === 'winter' ? 'Winter' : 'Sommer'),
        'wp_season_temp' => $temperature,
        'wp_heating_limit_temp' => $limit,
        'wp_season_source' => $source,
        'wp_season_hint' => $hint,
        'wp_season_temperature_source' => $temperature !== null ? $temperatureSource : null,
        'wp_season_reason' => $season !== '' ? '' : ($temperature === null ? 'outside_temperature_missing' : 'heating_limit_missing'),
    ];
}
