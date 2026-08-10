<?php
declare(strict_types=1);

/**
 * Eng begrenzter Read-only-Transport für eine E3DC-Control-Shadow-Instanz.
 *
 * Direkte HTTP-Zugriffe auf data/ und ramdisk/ bleiben durch Apache gesperrt.
 * Dieser Endpunkt veröffentlicht ausschließlich fest definierte, von
 * Geheimnissen bereinigte Projektionen. Er schreibt keine Datei und löst
 * keine Geräte-, Dienst- oder Hardwareaktion aus.
 */

const E3DC_SHADOW_SNAPSHOT_SCHEMA = 'e3dc_shadow_snapshot_v1';
const E3DC_SHADOW_BUNDLE_SCHEMA = 'e3dc_shadow_snapshot_bundle_v1';
const E3DC_SHADOW_BUNDLE_RESOURCE = 'bundle';
const E3DC_SHADOW_STORAGE_PLAN_SCHEMA = 'e3dc_shadow_storage_plan_projection_v1';
const E3DC_SHADOW_CONTRACT_HEADER = 'e3dc-shadow-read-v1';
const E3DC_SHADOW_TOKEN_HEADER = 'HTTP_X_E3DC_SHADOW_TOKEN';
const E3DC_SHADOW_CONFIG_FILE = '/var/www/html/data/e3dc_v4.json';
const E3DC_SHADOW_TOKEN_BYTES = 32;
const E3DC_SHADOW_RESPONSE_MAX_BYTES = 3 * 1024 * 1024;
const E3DC_SHADOW_BUNDLE_MAX_BYTES = 8 * 1024 * 1024;
const E3DC_SHADOW_PLAN_SOURCE_MAX_BYTES = 16 * 1024 * 1024;
const E3DC_SHADOW_CURVE_MAX_POINTS = 512;
const E3DC_SHADOW_DV_SUMMARY_MAX_BYTES = 64 * 1024;
const E3DC_SHADOW_DV_MAX_SLOTS = 400;

function e3dcShadowSnapshotResources(): array
{
    return [
        'live_data' => [
            'path' => '/var/www/html/ramdisk/live_data_py.json',
            'max_bytes' => E3DC_SHADOW_RESPONSE_MAX_BYTES,
            'projection' => 'live',
        ],
        'storage_state' => [
            'path' => '/var/www/html/ramdisk/storage_manager_state.json',
            'max_bytes' => E3DC_SHADOW_RESPONSE_MAX_BYTES,
            'projection' => 'storage_state',
        ],
        'storage_plan' => [
            'path' => '/var/www/html/ramdisk/storage_plan.json',
            'max_bytes' => E3DC_SHADOW_PLAN_SOURCE_MAX_BYTES,
            'projection' => 'storage_plan',
        ],
        'wb_budget' => [
            'path' => '/var/www/html/ramdisk/wb_pv_budget.json',
            'max_bytes' => E3DC_SHADOW_RESPONSE_MAX_BYTES,
            'projection' => 'wb_budget',
        ],
        'wb_intent' => [
            'path' => '/var/www/html/ramdisk/wallbox_storage_intent.json',
            'max_bytes' => E3DC_SHADOW_RESPONSE_MAX_BYTES,
            'projection' => 'wb_intent',
        ],
        'wallbox_native' => [
            'path' => '/var/www/html/ramdisk/wallbox_native.json',
            'max_bytes' => E3DC_SHADOW_RESPONSE_MAX_BYTES,
            'projection' => 'wallbox_native',
        ],
        'config' => [
            'path' => '/var/www/html/data/e3dc_v4.json',
            'max_bytes' => 4 * 1024 * 1024,
            'projection' => 'config',
        ],
    ];
}

function e3dcShadowSnapshotSecretKey(string $key): bool
{
    return preg_match(
        '/(?:password|passwort|passwd|pwd|token|secret|api[_-]?key|apikey|'
        . 'aes|credential|private[_-]?key|refresh[_-]?token|web[_-]?pin|'
        . 'chat[_-]?id|authorization|cookie)/iu',
        $key
    ) === 1;
}

function e3dcShadowSnapshotReadJson(string $path, int $maxBytes): array
{
    clearstatcache(true, $path);
    if (is_link($path)) {
        throw new RuntimeException('source_link_rejected');
    }

    $handle = @fopen($path, 'rb');
    if ($handle === false) {
        throw new RuntimeException('source_unavailable');
    }
    try {
        $metadata = @fstat($handle);
        if (
            !is_array($metadata)
            || (((int)($metadata['mode'] ?? 0)) & 0170000) !== 0100000
            || (int)($metadata['size'] ?? -1) < 0
            || (int)($metadata['size'] ?? 0) > $maxBytes
        ) {
            throw new RuntimeException('source_invalid');
        }
        $raw = stream_get_contents($handle, $maxBytes + 1);
        if (!is_string($raw) || strlen($raw) > $maxBytes) {
            throw new RuntimeException('source_too_large');
        }
        $afterRead = @fstat($handle);
        if (
            !is_array($afterRead)
            || (int)($afterRead['dev'] ?? -1) !== (int)($metadata['dev'] ?? -2)
            || (int)($afterRead['ino'] ?? -1) !== (int)($metadata['ino'] ?? -2)
            || (int)($afterRead['size'] ?? -1) !== (int)($metadata['size'] ?? -2)
            || (int)($afterRead['mtime'] ?? -1) !== (int)($metadata['mtime'] ?? -2)
            || strlen($raw) !== (int)($metadata['size'] ?? -1)
        ) {
            throw new RuntimeException('source_generation_changed_during_read');
        }

        clearstatcache(true, $path);
        $current = @lstat($path);
        if (
            !is_array($current)
            || (((int)($current['mode'] ?? 0)) & 0170000) !== 0100000
            || (int)($current['dev'] ?? -1) !== (int)($metadata['dev'] ?? -2)
            || (int)($current['ino'] ?? -1) !== (int)($metadata['ino'] ?? -2)
            || (int)($current['size'] ?? -1) !== (int)($metadata['size'] ?? -2)
            || (int)($current['mtime'] ?? -1) !== (int)($metadata['mtime'] ?? -2)
        ) {
            throw new RuntimeException('source_generation_changed');
        }
    } finally {
        fclose($handle);
    }

    $decoded = json_decode($raw, false, 512, JSON_BIGINT_AS_STRING);
    if (!is_object($decoded) || json_last_error() !== JSON_ERROR_NONE) {
        throw new RuntimeException('source_json_invalid');
    }
    return [
        'payload' => $decoded,
        'mtime' => (int)($metadata['mtime'] ?? 0),
        'bytes' => strlen($raw),
    ];
}

function e3dcShadowSnapshotTokenValid(string $token): bool
{
    return strlen($token) === E3DC_SHADOW_TOKEN_BYTES * 2
        && preg_match('/\A[0-9a-fA-F]{64}\z/D', $token) === 1;
}

/**
 * Liest ausschließlich das lokale Peer-Geheimnis. Diese Funktion muss im
 * Requestpfad vor jedem Zugriff auf eine Snapshot-Ressource aufgerufen werden.
 */
function e3dcShadowSnapshotLocalToken(): ?string
{
    try {
        $source = e3dcShadowSnapshotReadJson(E3DC_SHADOW_CONFIG_FILE, 4 * 1024 * 1024);
        $config = e3dcShadowSnapshotObjectMap($source['payload'] ?? null);
        $nested = e3dcShadowSnapshotObjectMap($config['config'] ?? null);
        $value = $config['shadow_snapshot_token'] ?? ($nested['shadow_snapshot_token'] ?? null);
        if (!is_string($value)) {
            return null;
        }
        $token = trim($value);
        return e3dcShadowSnapshotTokenValid($token) ? strtolower($token) : null;
    } catch (Throwable $error) {
        return null;
    }
}

/**
 * Getrennt testbare, konstantzeitliche Peer-Prüfung.
 *
 * @return int HTTP-Status 200, 403 oder 503
 */
function e3dcShadowSnapshotAuthorize(?string $localToken, ?string $requestToken): int
{
    if (!is_string($localToken) || !e3dcShadowSnapshotTokenValid($localToken)) {
        return 503;
    }
    $presented = is_string($requestToken) ? trim($requestToken) : '';
    if (!e3dcShadowSnapshotTokenValid($presented)) {
        return 403;
    }
    return hash_equals(strtolower($localToken), strtolower($presented)) ? 200 : 403;
}

function e3dcShadowSnapshotObjectMap($value): array
{
    if (is_object($value)) {
        return get_object_vars($value);
    }
    return is_array($value) ? $value : [];
}

function e3dcShadowSnapshotNumber($value)
{
    if (is_bool($value) || !is_numeric($value)) {
        return null;
    }
    $number = (float)$value;
    if (!is_finite($number)) {
        return null;
    }
    return is_int($value) ? $value : $number;
}

function e3dcShadowSnapshotBoolean($value): ?bool
{
    if (is_bool($value)) {
        return $value;
    }
    if ($value === 0 || $value === 1 || $value === '0' || $value === '1') {
        return (bool)((int)$value);
    }
    return null;
}

function e3dcShadowSnapshotCode($value): ?string
{
    if (!is_string($value)) {
        return null;
    }
    $code = trim($value);
    if (
        $code === ''
        || strlen($code) > 96
        || preg_match('/\A[A-Za-z0-9_.:+-]+\z/D', $code) !== 1
    ) {
        return null;
    }
    return $code;
}

function e3dcShadowSnapshotRevision($value): ?string
{
    if (
        !is_string($value)
        || preg_match('/\Asha256:[0-9a-fA-F]{64}\z/D', $value) !== 1
    ) {
        return null;
    }
    return strtolower($value);
}

function e3dcShadowSnapshotStrictNumber($value)
{
    if ((!is_int($value) && !is_float($value)) || !is_finite((float)$value)) {
        return null;
    }
    $number = (float)$value;
    if ($number < -1000000000000.0 || $number > 1000000000000.0) {
        return null;
    }
    return is_int($value) ? $value : $number;
}

function e3dcShadowSnapshotStrictInteger($value, int $maximum): ?int
{
    if (!is_int($value) || $value < 0 || $value > $maximum) {
        return null;
    }
    return $value;
}

function e3dcShadowSnapshotCodeList($value, int $maximumItems): array
{
    if (!is_array($value) || count($value) > $maximumItems) {
        return [];
    }
    $result = [];
    foreach ($value as $item) {
        $code = e3dcShadowSnapshotCode($item);
        if ($code !== null) {
            $result[] = $code;
        }
    }
    return $result;
}

function e3dcShadowSnapshotStrictScalars(
    $source,
    array $numericKeys,
    array $integerKeys,
    array $booleanKeys,
    array $codeKeys,
    array $revisionKeys
): array {
    $sourceMap = e3dcShadowSnapshotObjectMap($source);
    $result = [];
    foreach ($numericKeys as $key) {
        if (!array_key_exists($key, $sourceMap)) {
            continue;
        }
        if ($sourceMap[$key] === null) {
            $result[$key] = null;
            continue;
        }
        $value = e3dcShadowSnapshotStrictNumber($sourceMap[$key]);
        if ($value !== null) {
            $result[$key] = $value;
        }
    }
    foreach ($integerKeys as $key) {
        if (!array_key_exists($key, $sourceMap)) {
            continue;
        }
        if ($sourceMap[$key] === null) {
            $result[$key] = null;
            continue;
        }
        $value = e3dcShadowSnapshotStrictInteger(
            $sourceMap[$key],
            9000000000000000
        );
        if ($value !== null) {
            $result[$key] = $value;
        }
    }
    foreach ($booleanKeys as $key) {
        if (!array_key_exists($key, $sourceMap)) {
            continue;
        }
        if ($sourceMap[$key] === null) {
            $result[$key] = null;
            continue;
        }
        if (is_bool($sourceMap[$key])) {
            $result[$key] = $sourceMap[$key];
        }
    }
    foreach ($codeKeys as $key) {
        if (!array_key_exists($key, $sourceMap)) {
            continue;
        }
        if ($sourceMap[$key] === null) {
            $result[$key] = null;
            continue;
        }
        $value = e3dcShadowSnapshotCode($sourceMap[$key]);
        if ($value !== null) {
            $result[$key] = $value;
        }
    }
    foreach ($revisionKeys as $key) {
        if (!array_key_exists($key, $sourceMap)) {
            continue;
        }
        if ($sourceMap[$key] === null) {
            $result[$key] = null;
            continue;
        }
        $value = e3dcShadowSnapshotRevision($sourceMap[$key]);
        if ($value !== null) {
            $result[$key] = $value;
        }
    }
    return $result;
}

function e3dcShadowSnapshotDvActions(): array
{
    return [
        'HOUSE_SUPPLY',
        'PV_STORE',
        'CHARGE_BLOCK_WAIT',
        'GRID_CHARGE',
        'ECONOMIC_EXPORT',
    ];
}

function e3dcShadowSnapshotDvActionCounts($source, int $slotCount): ?array
{
    $sourceMap = e3dcShadowSnapshotObjectMap($source);
    $result = [];
    foreach (e3dcShadowSnapshotDvActions() as $action) {
        if (!array_key_exists($action, $sourceMap)) {
            return null;
        }
        $count = e3dcShadowSnapshotStrictInteger(
            $sourceMap[$action],
            E3DC_SHADOW_DV_MAX_SLOTS
        );
        if ($count === null) {
            return null;
        }
        $result[$action] = $count;
    }
    return array_sum($result) === $slotCount ? $result : null;
}

function e3dcShadowSnapshotDvValidationSummary(
    $source,
    int $slotCount
): ?array {
    $sourceMap = e3dcShadowSnapshotObjectMap($source);
    if (!$sourceMap) {
        return $slotCount === 0 ? [] : null;
    }
    $result = [];
    foreach (['slot_count', 'valid', 'tightened', 'rejected'] as $key) {
        if (!array_key_exists($key, $sourceMap)) {
            return null;
        }
        $value = e3dcShadowSnapshotStrictInteger(
            $sourceMap[$key],
            E3DC_SHADOW_DV_MAX_SLOTS
        );
        if ($value === null) {
            return null;
        }
        $result[$key] = $value;
    }
    if (
        $result['slot_count'] !== $slotCount
        || $result['valid'] + $result['tightened'] + $result['rejected']
            !== $slotCount
    ) {
        return null;
    }
    return $result;
}

function e3dcShadowSnapshotDvCompactSlot($source): ?array
{
    $sourceMap = e3dcShadowSnapshotObjectMap($source);
    if (!$sourceMap) {
        return null;
    }
    $result = e3dcShadowSnapshotStrictScalars(
        $source,
        [
            'active_policy_runtime_expected_max_charge_w',
            'effective_charge_cap_w',
            'effective_discharge_w',
            'projected_battery_w',
        ],
        ['start_ts_ms', 'end_ts_ms'],
        [
            'active_policy_runtime_candidate',
            'forecast_recommendation_applies',
        ],
        [
            'action',
            'purpose',
            'reason_code',
            'active_policy_runtime_evidence_status',
            'active_policy_runtime_reason_code',
            'forecast_recommendation_evidence_status',
            'forecast_recommendation_reason_code',
            'validation_status',
            'effective_action',
            'passive_power_source_contract',
        ],
        ['slot_id']
    );
    if (
        !isset(
            $result['slot_id'],
            $result['start_ts_ms'],
            $result['end_ts_ms'],
            $result['action']
        )
        || !in_array($result['action'], e3dcShadowSnapshotDvActions(), true)
        || $result['end_ts_ms'] - $result['start_ts_ms'] !== 900000
    ) {
        return null;
    }
    if (
        array_key_exists('effective_action', $result)
        && $result['effective_action'] !== null
        && !in_array(
            $result['effective_action'],
            e3dcShadowSnapshotDvActions(),
            true
        )
    ) {
        return null;
    }
    $result['reject_codes'] = e3dcShadowSnapshotCodeList(
        $sourceMap['reject_codes'] ?? null,
        16
    );
    $result['tighten_codes'] = e3dcShadowSnapshotCodeList(
        $sourceMap['tighten_codes'] ?? null,
        16
    );
    return $result;
}

function e3dcShadowSnapshotDvFutureHeadroom($source): ?array
{
    $sourceMap = e3dcShadowSnapshotObjectMap($source);
    if (!$sourceMap) {
        return null;
    }
    $result = e3dcShadowSnapshotStrictScalars(
        $source,
        ['independent_dc_point_forecast_wh'],
        ['start_ts_ms', 'end_ts_ms'],
        ['active', 'positive_precharge_bound', 'legacy_candidate_active'],
        ['evidence_status', 'reason_code'],
        ['revision']
    );
    if (
        !isset(
            $result['active'],
            $result['positive_precharge_bound'],
            $result['evidence_status'],
            $result['reason_code'],
            $result['start_ts_ms'],
            $result['end_ts_ms']
        )
        || $result['end_ts_ms'] < $result['start_ts_ms']
    ) {
        return null;
    }
    return $result;
}

function e3dcShadowSnapshotDvHardFloor($source): ?array
{
    $sourceMap = e3dcShadowSnapshotObjectMap($source);
    if (($sourceMap['schema_version'] ?? null) !== 'hard_physical_floor_v1') {
        return null;
    }
    $result = e3dcShadowSnapshotStrictScalars(
        $source,
        ['soc_pct', 'stored_wh'],
        [],
        ['immediate'],
        ['schema_version', 'source'],
        ['revision']
    );
    if (
        !isset(
            $result['schema_version'],
            $result['source'],
            $result['immediate'],
            $result['revision']
        )
        || (
            isset($result['soc_pct'])
            && ($result['soc_pct'] < 0.0 || $result['soc_pct'] > 100.0)
        )
        || (
            isset($result['stored_wh'])
            && $result['stored_wh'] < 0.0
        )
    ) {
        return null;
    }
    return $result;
}

function e3dcShadowSnapshotDvProtectedReserve($source): ?array
{
    $sourceMap = e3dcShadowSnapshotObjectMap($source);
    if (($sourceMap['schema_version'] ?? null) !== 'protected_demand_reserve_v1') {
        return null;
    }
    $result = e3dcShadowSnapshotStrictScalars(
        $source,
        ['required_stored_wh', 'current_stored_wh', 'shortfall_wh'],
        ['deadline_ts_ms'],
        [
            'current_requirement_met',
            'eligible_for_shadow_decision',
            'eligible_for_refill_decision',
        ],
        [
            'schema_version',
            'status',
            'reason_code',
            'refillability_evidence_status',
            'protection_semantics',
            'external_ac_storage_mode',
            'external_ac_storage_mode_source',
        ],
        ['revision']
    );
    if (
        !isset(
            $result['schema_version'],
            $result['status'],
            $result['reason_code'],
            $result['current_requirement_met'],
            $result['eligible_for_shadow_decision'],
            $result['eligible_for_refill_decision'],
            $result['revision']
        )
    ) {
        return null;
    }
    foreach (['required_stored_wh', 'current_stored_wh', 'shortfall_wh'] as $key) {
        if (isset($result[$key]) && $result[$key] < 0.0) {
            return null;
        }
    }
    return $result;
}

function e3dcShadowSnapshotDvSoftTarget($source): ?array
{
    $sourceMap = e3dcShadowSnapshotObjectMap($source);
    if (($sourceMap['schema_version'] ?? null) !== 'soft_charge_target_v1') {
        return null;
    }
    $result = e3dcShadowSnapshotStrictScalars(
        $source,
        ['target_soc_pct'],
        ['deadline_ts_ms'],
        ['hard_floor'],
        ['schema_version', 'source'],
        ['revision']
    );
    if (
        !isset(
            $result['schema_version'],
            $result['hard_floor'],
            $result['source'],
            $result['revision']
        )
        || (
            isset($result['target_soc_pct'])
            && (
                $result['target_soc_pct'] < 0.0
                || $result['target_soc_pct'] > 100.0
            )
        )
    ) {
        return null;
    }
    return $result;
}

function e3dcShadowSnapshotDvReserveClasses($source): ?array
{
    $sourceMap = e3dcShadowSnapshotObjectMap($source);
    if (!$sourceMap) {
        return null;
    }
    $result = [];
    $hardFloor = e3dcShadowSnapshotDvHardFloor(
        $sourceMap['hard_physical_floor'] ?? null
    );
    if ($hardFloor !== null) {
        $result['hard_physical_floor'] = $hardFloor;
    }
    $protectedReserve = e3dcShadowSnapshotDvProtectedReserve(
        $sourceMap['protected_demand_reserve'] ?? null
    );
    if ($protectedReserve !== null) {
        $result['protected_demand_reserve'] = $protectedReserve;
    }
    $softTarget = e3dcShadowSnapshotDvSoftTarget(
        $sourceMap['soft_charge_target'] ?? null
    );
    if ($softTarget !== null) {
        $result['soft_charge_target'] = $softTarget;
    }
    return $result ?: null;
}

function e3dcShadowSnapshotDvSummary($source): ?array
{
    $sourceMap = e3dcShadowSnapshotObjectMap($source);
    if (
        ($sourceMap['schema_version'] ?? null)
            !== 'direct_marketing_dispatch_shadow_v1'
        || ($sourceMap['representation'] ?? null) !== 'COMPACT_SUMMARY'
        || ($sourceMap['shadow_only'] ?? null) !== true
        || ($sourceMap['commands_allowed'] ?? null) !== false
        || ($sourceMap['runtime_owner'] ?? null) !== 'storage_manager'
    ) {
        return null;
    }
    $status = e3dcShadowSnapshotCode($sourceMap['status'] ?? null);
    if (!in_array(
        $status,
        ['VALID', 'VALID_TIGHTENED', 'REJECTED', 'NOT_APPLICABLE', 'SHADOW_ERROR'],
        true
    )) {
        return null;
    }
    $slotCount = e3dcShadowSnapshotStrictInteger(
        $sourceMap['slot_count'] ?? null,
        E3DC_SHADOW_DV_MAX_SLOTS
    );
    $planComplete = $sourceMap['plan_complete'] ?? null;
    $algorithm = e3dcShadowSnapshotCode($sourceMap['algorithm'] ?? null);
    $summaryId = e3dcShadowSnapshotRevision($sourceMap['summary_id'] ?? null);
    if (
        $slotCount === null
        || !is_bool($planComplete)
        || $algorithm === null
        || $summaryId === null
    ) {
        return null;
    }
    $actionCounts = e3dcShadowSnapshotDvActionCounts(
        $sourceMap['action_counts'] ?? null,
        $slotCount
    );
    $validationSummary = e3dcShadowSnapshotDvValidationSummary(
        $sourceMap['validation_summary'] ?? null,
        $slotCount
    );
    if ($actionCounts === null || $validationSummary === null) {
        return null;
    }

    $summary = [
        'schema_version' => 'direct_marketing_dispatch_shadow_v1',
        'representation' => 'COMPACT_SUMMARY',
        'algorithm' => $algorithm,
        'shadow_only' => true,
        'commands_allowed' => false,
        'runtime_owner' => 'storage_manager',
        'status' => $status,
    ];
    foreach (
        [
            'planning_input_revision',
            'dv_plan_revision',
            'physics_validation_revision',
        ] as $key
    ) {
        if (!array_key_exists($key, $sourceMap)) {
            return null;
        }
        if ($sourceMap[$key] === null) {
            $summary[$key] = null;
            continue;
        }
        $revision = e3dcShadowSnapshotRevision($sourceMap[$key]);
        if ($revision === null) {
            return null;
        }
        $summary[$key] = $revision;
    }
    if (!in_array($status, ['NOT_APPLICABLE', 'SHADOW_ERROR'], true)) {
        foreach (
            [
                'planning_input_revision',
                'dv_plan_revision',
                'physics_validation_revision',
            ] as $key
        ) {
            if ($summary[$key] === null) {
                return null;
            }
        }
    }

    $summary['plan_complete'] = $planComplete;
    $summary['slot_count'] = $slotCount;
    $summary['action_counts'] = $actionCounts;
    $summary['validation_summary'] = (object)$validationSummary;
    $summary['reject_codes'] = e3dcShadowSnapshotCodeList(
        $sourceMap['reject_codes'] ?? null,
        32
    );
    $summary['tighten_codes'] = e3dcShadowSnapshotCodeList(
        $sourceMap['tighten_codes'] ?? null,
        32
    );
    if (array_key_exists('reason_code', $sourceMap)) {
        $reasonCode = e3dcShadowSnapshotCode($sourceMap['reason_code']);
        if ($reasonCode !== null) {
            $summary['reason_code'] = $reasonCode;
        }
    }

    foreach (['current_slot', 'next_transition'] as $key) {
        if (($sourceMap[$key] ?? null) === null) {
            $summary[$key] = null;
            continue;
        }
        $slot = e3dcShadowSnapshotDvCompactSlot($sourceMap[$key]);
        if ($slot === null) {
            return null;
        }
        $summary[$key] = $slot;
    }
    if (array_key_exists('future_headroom_hold_evidence', $sourceMap)) {
        $futureHeadroom = e3dcShadowSnapshotDvFutureHeadroom(
            $sourceMap['future_headroom_hold_evidence']
        );
        if ($futureHeadroom !== null) {
            $summary['future_headroom_hold_evidence'] = $futureHeadroom;
        }
    }
    if (array_key_exists('reserve_classes', $sourceMap)) {
        $reserveClasses = e3dcShadowSnapshotDvReserveClasses(
            $sourceMap['reserve_classes']
        );
        if ($reserveClasses !== null) {
            $summary['reserve_classes'] = $reserveClasses;
        }
    }

    $persisted = $sourceMap['full_payload_persisted'] ?? null;
    if ($persisted !== null && $persisted !== false) {
        return null;
    }
    $summary['full_payload_persisted'] = $persisted;
    $summary['summary_id'] = $summaryId;
    $encoded = json_encode(
        $summary,
        JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_PRESERVE_ZERO_FRACTION
    );
    if (
        !is_string($encoded)
        || strlen($encoded) > E3DC_SHADOW_DV_SUMMARY_MAX_BYTES
    ) {
        return null;
    }
    return $summary;
}

function e3dcShadowSnapshotTypedScalars(
    $source,
    array $numericKeys,
    array $booleanKeys,
    array $codeKeys
): array {
    $sourceMap = e3dcShadowSnapshotObjectMap($source);
    $result = [];
    foreach ($numericKeys as $key) {
        if (!array_key_exists($key, $sourceMap)) {
            continue;
        }
        $value = e3dcShadowSnapshotNumber($sourceMap[$key]);
        if ($value !== null) {
            $result[$key] = $value;
        }
    }
    foreach ($booleanKeys as $key) {
        if (!array_key_exists($key, $sourceMap)) {
            continue;
        }
        $value = e3dcShadowSnapshotBoolean($sourceMap[$key]);
        if ($value !== null) {
            $result[$key] = $value;
        }
    }
    foreach ($codeKeys as $key) {
        if (!array_key_exists($key, $sourceMap)) {
            continue;
        }
        $value = e3dcShadowSnapshotCode($sourceMap[$key]);
        if ($value !== null) {
            $result[$key] = $value;
        }
    }
    return $result;
}

function e3dcShadowSnapshotLive($source): array
{
    $numeric = [
        '_ts', 'ts', 'timestamp', 'timestamp_s', 'last_update_ts', 'last_update', 'time',
        'SOC', 'PV_Power', 'Grid_Power', 'Battery_Power', 'Home_Power',
        'Wallbox_Power', 'WP_Power', 'Heatpump_Power', 'Ext_PV_Power',
        'user_charge_limit_w', 'bat_charge_limit_w', 'user_discharge_limit_w',
        'bat_discharge_limit_w', 'derate_at_power_w', 'ems_max_charge_power_w',
        'ems_max_discharge_power_w', 'ems_discharge_start_power_w',
        'ac_power_limit_w', 'wr_ac_limit_w', 'inverter_ac_limit_w',
        'dc0_max_w', 'dc1_max_w', 'dc2_max_w', 'dc3_max_w',
        'dc4_max_w', 'dc5_max_w', 'dc6_max_w', 'dc7_max_w',
    ];
    $booleans = [
        'Ext_PV_Power_Valid', 'Wallbox_Home_Includes', 'RSCP_Sample_Valid',
        'Grid_Power_Valid', 'pv_derating_active', 'ems_derating_active',
        'ems_power_settings_read', 'ems_power_settings_valid', 'power_limits_active',
    ];
    return e3dcShadowSnapshotTypedScalars(
        $source,
        $numeric,
        $booleans,
        ['Ext_PV_Power_Source']
    );
}

function e3dcShadowSnapshotStorageState($source): array
{
    $numeric = [
        'adaptive_headroom_available_wh', 'adaptive_headroom_required_wh', 'bat_w',
        'curtailment_pressure_wh', 'curve_cap_post_release_until_ts',
        'curve_cap_release_below_since_ts', 'curve_cap_release_confirmed_since_ts',
        'curve_gap_catchup_cap_w', 'curve_gap_catchup_factor',
        'curve_gap_catchup_min_w', 'curve_gap_catchup_taper_pct',
        'curve_gap_catchup_w', 'curve_gap_pct', 'curve_need_raw_w',
        'derate_at_power_w', 'ep_reserve_pct', 'forecast_floor_target_gap_pct',
        'forecast_landing_margin_pct', 'grid_ema_w', 'grid_w',
        'headroom_discharge_last_account_ts', 'headroom_discharge_last_active_ts',
        'headroom_discharge_today_wh', 'headroom_reserve_pressure_wh', 'home_ema_w',
        'iAVal_w', 'iFc_w', 'iMinLade_w', 'last_auto_ts', 'mode',
        'last_wb_active_ts', 'last_wb_possible_power_w', 'lookahead_need_w',
        'now_ts_s', 'planned_load_expected_w', 'planned_load_observed_extra_w',
        'previous_parallel_mode', 'previous_parallel_ts', 'previous_parallel_val',
        'previous_state_ts',
        'shortfall_catchup_enter_w', 'shortfall_catchup_nominal_enter_w',
        'shortfall_late_catchup_enter_w', 'shortfall_real_surplus_w',
        'shortfall_target_gap_pct', 'shortfall_target_soc',
        'sliding_horizon_confidence', 'sliding_horizon_headroom_available_wh',
        'sliding_horizon_min_confidence', 'sliding_horizon_minutes_until_latest_charge',
        'sliding_horizon_uncovered_curtailment_pressure_wh',
        'sliding_horizon_uncovered_pressure_wh', 'soc', 'val',
        'wb_possible_power_w',
    ];
    $booleans = [
        'Wallbox_Home_Includes', 'curve_cap_release_pending',
        'curve_cap_release_requested', 'ems_derating_active',
        'forecast_curve_landing_hold_active', 'headroom_reserve_active',
        'planned_load_confirmed', 'planned_load_support_allowed',
        'power_limits_active', 'pv_derating_active',
        'shortfall_catchup_blocked_curve_ready',
        'shortfall_catchup_blocked_low_surplus', 'shortfall_catchup_curve_pressure',
        'shortfall_late_catchup_active', 'shortfall_pv_catchup_active',
        'sliding_horizon_active', 'sliding_horizon_candidate_active',
        'sliding_horizon_corridor_veto',
    ];
    $codes = [
        'headroom_discharge_day', 'headroom_reserve_source', 'last_parallel_state',
        'planned_load_mode', 'planned_load_support_reason', 'previous_parallel_state',
        'sliding_horizon_reason', 'sliding_horizon_season', 'state', 'storage_state',
    ];
    $projection = e3dcShadowSnapshotTypedScalars($source, $numeric, $booleans, $codes);

    $sourceMap = e3dcShadowSnapshotObjectMap($source);
    $headroom = e3dcShadowSnapshotTypedScalars(
        $sourceMap['headroom_execution'] ?? null,
        ['residual_wh', 'target_soc', 'hard_floor_soc'],
        ['allowed'],
        ['schema_version', 'reason_code']
    );
    if ($headroom) {
        $projection['headroom_execution'] = $headroom;
    }
    $plannedLoadSupport = e3dcShadowSnapshotTypedScalars(
        $sourceMap['planned_load_support'] ?? null,
        ['support_max_discharge_w'],
        ['allowed'],
        ['reason', 'mode']
    );
    if ($plannedLoadSupport) {
        $projection['planned_load_support'] = $plannedLoadSupport;
    }
    return $projection;
}

function e3dcShadowSnapshotWallboxBudget($source): array
{
    return e3dcShadowSnapshotTypedScalars(
        $source,
        ['budget_w', 'wb_possible_power_w', 'iAVal_w'],
        [],
        []
    );
}

function e3dcShadowSnapshotWallboxIntent($source): array
{
    return e3dcShadowSnapshotTypedScalars(
        $source,
        ['cap_amp', 'set_amp', 'ts', 'wb_mode_active', 'wb_power_w'],
        [
            'active', 'autonomous_wallbox', 'bev_full_blocked', 'car_active',
            'charging_active', 'connected', 'external_wallbox_manager',
            'openwb_primary_observe_only', 'plugged', 'price_boost_active',
            'price_plan_storage_protect', 'scheduled_slot_active',
            'start_request_authorized', 'start_requested',
        ],
        ['battery_request', 'start_request_contract_version']
    );
}

function e3dcShadowSnapshotWallboxNative($source): array
{
    $sourceMap = e3dcShadowSnapshotObjectMap($source);
    $projection = e3dcShadowSnapshotTypedScalars(
        $source,
        ['total_power_w', 'power_w', 'wb_total_w'],
        [],
        []
    );
    $rows = isset($sourceMap['wb_details']) && is_array($sourceMap['wb_details'])
        ? $sourceMap['wb_details']
        : [];
    $details = [];
    foreach (array_slice($rows, 0, 8) as $row) {
        $projected = e3dcShadowSnapshotTypedScalars(
            $row,
            [],
            ['connected', 'car_connected'],
            []
        );
        if ($projected) {
            $details[] = $projected;
        }
    }
    if ($details) {
        $projection['wb_details'] = $details;
    }
    return $projection;
}

function e3dcShadowSnapshotCurve($source, string $key, ?string $fallbackKey = null): array
{
    $sourceMap = e3dcShadowSnapshotObjectMap($source);
    $rows = isset($sourceMap[$key]) && is_array($sourceMap[$key])
        ? $sourceMap[$key]
        : [];
    if (
        !$rows
        && $fallbackKey !== null
        && isset($sourceMap[$fallbackKey])
        && is_array($sourceMap[$fallbackKey])
    ) {
        $rows = $sourceMap[$fallbackKey];
    }
    if (count($rows) > E3DC_SHADOW_CURVE_MAX_POINTS) {
        throw new RuntimeException('storage_plan_curve_too_large');
    }

    $result = [];
    foreach ($rows as $row) {
        $rowMap = e3dcShadowSnapshotObjectMap($row);
        if (!$rowMap || !isset($rowMap['ts']) || !is_numeric($rowMap['ts'])) {
            continue;
        }
        $socValue = $rowMap['soc'] ?? ($rowMap['target_soc'] ?? null);
        if (!is_numeric($socValue)) {
            continue;
        }
        $ts = (int)round((float)$rowMap['ts']);
        $soc = (float)$socValue;
        if ($ts <= 0 || !is_finite($soc) || $soc < 0.0 || $soc > 100.0) {
            continue;
        }
        $result[] = [
            'ts' => $ts,
            'soc' => round($soc, 4),
        ];
    }
    return $result;
}

function e3dcShadowSnapshotStoragePlan($source): array
{
    $sourceMap = e3dcShadowSnapshotObjectMap($source);
    $projection = e3dcShadowSnapshotTypedScalars(
        $source,
        [
            'generated_at_ts_ms', 'valid_from_ts_ms', 'valid_until_ts_ms',
            'horizon_end_ts_ms', 'slot_duration_s', 'max_reachable_soc',
        ],
        ['active', 'can_reach_target'],
        ['schema_version', 'plan_id']
    );

    $projection['target_timeline'] = e3dcShadowSnapshotCurve(
        $source,
        'target_timeline',
        'timeline'
    );
    $projection['soc_min_curve'] = e3dcShadowSnapshotCurve($source, 'soc_min_curve');
    $projection['soc_ceiling_curve'] = e3dcShadowSnapshotCurve($source, 'soc_ceiling_curve');

    $targetMeta = e3dcShadowSnapshotObjectMap($sourceMap['target_curve_meta'] ?? null);
    $projection['target_curve_meta'] = e3dcShadowSnapshotTypedScalars(
        $targetMeta,
        [],
        ['forecast_only_target_active'],
        ['target_mode']
    );

    $planner = e3dcShadowSnapshotObjectMap($sourceMap['planner'] ?? null);
    $dvSummary = e3dcShadowSnapshotDvSummary(
        $planner['dv_shadow_v1'] ?? null
    );
    if ($dvSummary !== null) {
        $projection['planner'] = ['dv_shadow_v1' => $dvSummary];
    }

    $projection['shadow_transport'] = [
        'schema_version' => E3DC_SHADOW_STORAGE_PLAN_SCHEMA,
        'source_plan_id' => e3dcShadowSnapshotCode($sourceMap['plan_id'] ?? null),
        'source_slot_count' => isset($sourceMap['slots']) && is_array($sourceMap['slots'])
            ? count($sourceMap['slots'])
            : 0,
        'target_point_count' => count($projection['target_timeline']),
        'floor_point_count' => count($projection['soc_min_curve']),
        'ceiling_point_count' => count($projection['soc_ceiling_curve']),
        'full_slots_included' => false,
        'commands_allowed' => false,
        'control_effect' => false,
    ];
    return $projection;
}

function e3dcShadowSnapshotConfigKeys(): array
{
    return [
        'maximumladeleistung',
        'maximaleentladeleistung',
        'speichergroesse',
        'einspeiselimit',
        'ep_reserve_pct',
        'wr_ac_limit_w',
        'wechselrichter_limit_w',
        'wechselrichterleistung_w',
        'inverter_ac_limit_w',
        'ac_power_limit_w',
        'abregel_auto_band_w',
        'abregel_auto_grace_s',
        'abregel_hysterese_w',
        'abregel_min_charge_w',
        'abregel_puffer_w',
        'storage_target_soc',
        'storage_curve_charge_servo_deadband_w',
        'storage_curve_charge_servo_max_age_s',
        'storage_curve_charge_servo_min_w',
        'storage_curve_charge_servo_mode',
        'storage_curve_charge_servo_step_down_w',
        'storage_curve_charge_servo_step_up_w',
        'storage_curve_shortfall_release_margin_pct',
        'storage_headroom_discharge_cooldown_min',
        'storage_headroom_discharge_daily_limit_pct',
        'storage_headroom_discharge_energy_gap_s',
        'storage_headroom_discharge_enter_pct',
        'storage_headroom_discharge_export_margin_w',
        'storage_headroom_discharge_horizon_h',
        'storage_headroom_discharge_import_guard_w',
        'storage_headroom_discharge_keep_pct',
        'storage_headroom_discharge_max_w',
        'storage_headroom_discharge_min_pressure_wh',
        'storage_headroom_discharge_min_pv_w',
        'storage_headroom_discharge_min_w',
        'storage_headroom_discharge_step_w',
        'storage_headroom_discharge_target_plateau_margin_pct',
        'storage_parallel_auto_hold_s',
        'storage_parallel_curve_auto_hold_exit_pct',
        'storage_parallel_curve_auto_hold_release_below_pct',
        'storage_parallel_curve_cap_enter_margin_w',
        'storage_parallel_curve_cap_export_trigger_w',
        'storage_parallel_curve_cap_feedback_band_w',
        'storage_parallel_curve_cap_keep_margin_w',
        'storage_parallel_curve_cap_short_hold_s',
        'storage_parallel_curve_cap_step_w',
        'storage_parallel_curve_charge_enter_w',
        'storage_parallel_curve_charge_keep_w',
        'storage_parallel_curve_charge_reenter_w',
        'storage_parallel_curve_charge_release_stabilize_s',
        'storage_parallel_curve_charge_soc_step_hold_s',
        'storage_parallel_curve_edge_soft_factor',
        'storage_parallel_curve_edge_soft_hold_s',
        'storage_parallel_curve_guard_enter_below_pct',
        'storage_parallel_curve_tolerance_pct',
        'storage_parallel_diff_log_interval_s',
        'storage_parallel_diff_min_w',
        'storage_parallel_grid_relief_enter_w',
        'storage_parallel_history_max_lines',
        'storage_parallel_night_floor_enter_pct',
        'storage_parallel_night_floor_keep_pct',
        'storage_parallel_pre_curve_hold_margin_pct',
        'storage_parallel_pre_curve_ifc_start_w',
        'storage_parallel_price_house_discharge_enter_w',
        'storage_parallel_price_house_discharge_keep_w',
        'storage_parallel_price_house_step_down_w',
        'storage_parallel_price_house_step_up_w',
        'storage_parallel_wb_auto_grid_abort_w',
        'storage_parallel_wb_hold_s',
        'storage_parallel_wb_owner_real_min_w',
        'tl_grid_limit_w',
        'tl_tolerance_pct',
        'wb_native_enable',
        'wb_native_type',
        'wb_native_type2',
        'wb1_mode',
        'wb2_mode',
        'wbmaxladestrom',
        'wb_max_amp',
        'wb_max_phases',
        'vehicle_max_phases',
        'wb_phase_down_delay_s',
        'wb_phase_down_grid_w',
        'wb_phase_down_reup_block_s',
        'wb_phase_up_buffer_w',
        'wb_phase_up_forecast_hold_s',
        'wb_shadow_amp_deadband_a',
        'wb_shadow_fast_grid_s',
        'wb_shadow_fast_grid_w',
        'wb_shadow_meter_delay_s',
        'wb_shadow_meter_ramp_s',
        'wb_shadow_phase_pause_s',
        'wb_shadow_power_ramp_s',
        'wb_shadow_start_delay_s',
        'wb_shadow_zero_budget_grid_stop_s',
        'wb_shadow_zero_budget_restart_block_s',
        'wb_shadow_zero_budget_stop_s',
        'wb_stable_budget_jump_deadband_a',
        'wb_stable_budget_jump_hold_s',
        'wb_stable_budget_jump_max_a',
        'wb_stable_follow_hold_s',
        'wb_stable_start_confirm_w',
    ];
}

function e3dcShadowSnapshotConfig($source): array
{
    $allowed = array_fill_keys(e3dcShadowSnapshotConfigKeys(), true);
    $projection = [];
    $visit = function ($node, int $depth = 0) use (&$visit, &$projection, $allowed): void {
        if ($depth > 4) {
            return;
        }
        foreach (e3dcShadowSnapshotObjectMap($node) as $key => $value) {
            $normalized = strtolower(trim((string)$key));
            if ($normalized === '' || e3dcShadowSnapshotSecretKey($normalized)) {
                continue;
            }
            if (isset($allowed[$normalized])) {
                if ($value === null || $value === '') {
                    $projection[$normalized] = $value;
                    continue;
                }
                $boolean = e3dcShadowSnapshotBoolean($value);
                if (is_bool($value) && $boolean !== null) {
                    $projection[$normalized] = $boolean;
                    continue;
                }
                if (is_string($value) && is_numeric(trim($value))) {
                    $projection[$normalized] = trim($value);
                    continue;
                }
                $number = e3dcShadowSnapshotNumber($value);
                if ($number !== null) {
                    $projection[$normalized] = $number;
                    continue;
                }
                $code = e3dcShadowSnapshotCode($value);
                if ($code !== null) {
                    $projection[$normalized] = $code;
                }
                continue;
            }
            if (is_array($value) || is_object($value)) {
                $visit($value, $depth + 1);
            }
        }
    };
    $visit($source);
    ksort($projection);
    return $projection;
}

function e3dcShadowSnapshotProject(array $binding, array $source)
{
    $projection = (string)($binding['projection'] ?? '');
    if ($projection === 'live') {
        $payload = e3dcShadowSnapshotLive($source['payload'] ?? null);
    } elseif ($projection === 'storage_state') {
        $payload = e3dcShadowSnapshotStorageState($source['payload'] ?? null);
    } elseif ($projection === 'storage_plan') {
        $payload = e3dcShadowSnapshotStoragePlan($source['payload'] ?? null);
    } elseif ($projection === 'wb_budget') {
        $payload = e3dcShadowSnapshotWallboxBudget($source['payload'] ?? null);
    } elseif ($projection === 'wb_intent') {
        $payload = e3dcShadowSnapshotWallboxIntent($source['payload'] ?? null);
    } elseif ($projection === 'wallbox_native') {
        $payload = e3dcShadowSnapshotWallboxNative($source['payload'] ?? null);
    } elseif ($projection === 'config') {
        $payload = e3dcShadowSnapshotConfig($source['payload'] ?? null);
    } else {
        throw new RuntimeException('projection_invalid');
    }
    if (!is_array($payload) && !is_object($payload)) {
        throw new RuntimeException('projection_invalid');
    }
    return $payload;
}

function e3dcShadowSnapshotPayloadRaw($payload): string
{
    $payloadRaw = json_encode(
        $payload,
        JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_PRESERVE_ZERO_FRACTION
    );
    if (!is_string($payloadRaw)) {
        throw new RuntimeException('projection_encoding_failed');
    }
    if (strlen($payloadRaw) > E3DC_SHADOW_RESPONSE_MAX_BYTES) {
        throw new RuntimeException('projection_too_large');
    }
    return $payloadRaw;
}

function e3dcShadowSnapshotErrorCode(Throwable $error): string
{
    $code = (string)$error->getMessage();
    $allowed = [
        'source_link_rejected',
        'source_unavailable',
        'source_invalid',
        'source_too_large',
        'source_generation_changed_during_read',
        'source_generation_changed',
        'source_json_invalid',
        'storage_plan_curve_too_large',
        'projection_invalid',
        'projection_encoding_failed',
        'projection_too_large',
    ];
    return in_array($code, $allowed, true) ? $code : 'resource_unavailable';
}

/**
 * Bündelt ausschließlich die fest vorgegebenen Ressourcen. Ein Teilfehler
 * liefert keinen Payload und kann deshalb nicht als frischer Wert erscheinen.
 */
function e3dcShadowSnapshotBundle(array $resources): array
{
    $sourceCache = [];
    $items = [];
    $okCount = 0;
    $errorCount = 0;

    foreach ($resources as $resource => $binding) {
        try {
            if (
                !is_string($resource)
                || !is_array($binding)
                || !isset($binding['path'], $binding['max_bytes'], $binding['projection'])
            ) {
                throw new RuntimeException('source_invalid');
            }
            $cacheKey = (string)$binding['path'] . "\0" . (string)(int)$binding['max_bytes'];
            if (!array_key_exists($cacheKey, $sourceCache)) {
                $sourceCache[$cacheKey] = e3dcShadowSnapshotReadJson(
                    (string)$binding['path'],
                    (int)$binding['max_bytes']
                );
            }
            $source = $sourceCache[$cacheKey];
            $payload = e3dcShadowSnapshotProject($binding, $source);
            $payloadRaw = e3dcShadowSnapshotPayloadRaw($payload);
            $items[$resource] = [
                'schema_version' => E3DC_SHADOW_SNAPSHOT_SCHEMA,
                'resource' => $resource,
                'ok' => true,
                'source_mtime_ts' => (int)($source['mtime'] ?? 0),
                'payload_encoding' => 'base64-json-utf8',
                'payload_bytes' => strlen($payloadRaw),
                'payload_sha256' => 'sha256:' . hash('sha256', $payloadRaw),
                'payload_base64' => base64_encode($payloadRaw),
            ];
            $okCount++;
        } catch (Throwable $error) {
            $items[$resource] = [
                'schema_version' => E3DC_SHADOW_SNAPSHOT_SCHEMA,
                'resource' => $resource,
                'ok' => false,
                'error_code' => e3dcShadowSnapshotErrorCode($error),
            ];
            $errorCount++;
        }
    }

    return [
        'schema_version' => E3DC_SHADOW_BUNDLE_SCHEMA,
        'resource' => E3DC_SHADOW_BUNDLE_RESOURCE,
        'generated_at_ts' => time(),
        'shadow_only' => true,
        'commands_allowed' => false,
        'control_effect' => false,
        'complete' => $errorCount === 0,
        'resource_count' => count($items),
        'ok_count' => $okCount,
        'error_count' => $errorCount,
        'resources' => $items,
    ];
}

function e3dcShadowSnapshotEncode(array $payload, int $maxBytes): string
{
    $encoded = json_encode(
        $payload,
        JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_PRESERVE_ZERO_FRACTION
    );
    if (!is_string($encoded) || strlen($encoded) > $maxBytes) {
        throw new RuntimeException('projection_too_large');
    }
    return $encoded;
}

function e3dcShadowSnapshotEncodedResponse(int $status, string $encoded): void
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store, max-age=0');
    header('Pragma: no-cache');
    header('X-Content-Type-Options: nosniff');
    header('Referrer-Policy: no-referrer');
    header('Content-Length: ' . strlen($encoded));
    echo $encoded;
}

function e3dcShadowSnapshotJsonResponse(int $status, array $payload): void
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store, max-age=0');
    header('Pragma: no-cache');
    header('X-Content-Type-Options: nosniff');
    header('Referrer-Policy: no-referrer');
    echo json_encode(
        $payload,
        JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_PRESERVE_ZERO_FRACTION
    );
}

function e3dcShadowSnapshotHandleRequest(): void
{
    if (strtoupper((string)($_SERVER['REQUEST_METHOD'] ?? 'GET')) !== 'GET') {
        header('Allow: GET');
        e3dcShadowSnapshotJsonResponse(405, ['error' => 'method_not_allowed']);
        return;
    }
    if (trim((string)($_SERVER['HTTP_ORIGIN'] ?? '')) !== '') {
        e3dcShadowSnapshotJsonResponse(403, ['error' => 'browser_origin_rejected']);
        return;
    }
    // Der Contract-Header bindet die Protokollversion; die Peer-Authentisierung
    // erfolgt getrennt über das lokale, niemals projizierte Snapshot-Token.
    if (
        (string)($_SERVER['HTTP_X_E3DC_SHADOW_CONTRACT'] ?? '')
        !== E3DC_SHADOW_CONTRACT_HEADER
    ) {
        e3dcShadowSnapshotJsonResponse(403, ['error' => 'shadow_contract_required']);
        return;
    }

    // Erster und einziger Dateizugriff bis zur erfolgreichen Authentisierung:
    // das lokale Peer-Geheimnis. Snapshot-Ressourcen werden vorher nie geöffnet.
    $authorization = e3dcShadowSnapshotAuthorize(
        e3dcShadowSnapshotLocalToken(),
        isset($_SERVER[E3DC_SHADOW_TOKEN_HEADER])
            ? (string)$_SERVER[E3DC_SHADOW_TOKEN_HEADER]
            : null
    );
    if ($authorization === 503) {
        e3dcShadowSnapshotJsonResponse(503, ['error' => 'shadow_snapshot_unavailable']);
        return;
    }
    if ($authorization !== 200) {
        e3dcShadowSnapshotJsonResponse(403, ['error' => 'forbidden']);
        return;
    }

    if (count($_GET) !== 1 || !isset($_GET['resource']) || !is_string($_GET['resource'])) {
        e3dcShadowSnapshotJsonResponse(400, ['error' => 'invalid_request']);
        return;
    }

    $resource = trim($_GET['resource']);
    $resources = e3dcShadowSnapshotResources();
    $isBundle = $resource === E3DC_SHADOW_BUNDLE_RESOURCE;
    if (!$isBundle && !isset($resources[$resource])) {
        e3dcShadowSnapshotJsonResponse(404, ['error' => 'resource_not_allowed']);
        return;
    }

    try {
        if ($isBundle) {
            $bundle = e3dcShadowSnapshotBundle($resources);
            $encoded = e3dcShadowSnapshotEncode($bundle, E3DC_SHADOW_BUNDLE_MAX_BYTES);
            e3dcShadowSnapshotEncodedResponse(200, $encoded);
            return;
        }

        $binding = $resources[$resource];
        $source = e3dcShadowSnapshotReadJson(
            (string)$binding['path'],
            (int)$binding['max_bytes']
        );
        $payload = e3dcShadowSnapshotProject($binding, $source);
        $payloadRaw = e3dcShadowSnapshotPayloadRaw($payload);
        $envelope = [
            'schema_version' => E3DC_SHADOW_SNAPSHOT_SCHEMA,
            'resource' => $resource,
            'generated_at_ts' => time(),
            'source_mtime_ts' => (int)$source['mtime'],
            'payload_sha256' => 'sha256:' . hash('sha256', $payloadRaw),
            'shadow_only' => true,
            'commands_allowed' => false,
            'control_effect' => false,
            'payload' => $payload,
        ];
        $encoded = e3dcShadowSnapshotEncode($envelope, E3DC_SHADOW_RESPONSE_MAX_BYTES);
        e3dcShadowSnapshotEncodedResponse(200, $encoded);
    } catch (Throwable $error) {
        e3dcShadowSnapshotJsonResponse(503, [
            'error' => 'shadow_snapshot_unavailable',
            'resource' => $resource,
        ]);
    }
}

if (!defined('E3DC_SHADOW_SNAPSHOT_LIBRARY_ONLY')) {
    e3dcShadowSnapshotHandleRequest();
}
