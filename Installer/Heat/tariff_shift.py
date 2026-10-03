"""Reine Wärmeplanung für positive Tariffenster; keine Hardwarezugriffe.

Alle Energiemengen bezeichnen elektrischen WP-Verbrauch, keine thermischen kWh.
Fehlende Evidenz bleibt unbekannt und autorisiert keinen Start.
"""
import copy
import hashlib
import json
import math
from datetime import datetime, timedelta

from .price_boost import parse_allowed_windows, normalize_scope, heating_boost_allowed
try:
    from ..tariff_schedule import TARIFF_TIMEZONE
except ImportError:
    from tariff_schedule import TARIFF_TIMEZONE


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def enabled(value):
    return str(value).lower() in ('1', 'true', 'on')


def migrate(config):
    result = dict(config)
    result.setdefault('heat_tariff_shift_mode', 'off')
    result.setdefault('heat_tariff_shift_windows', '02:00-06:00\n12:00-16:00')
    result.setdefault('heat_tariff_shift_ww_lead_min', 90)
    result.setdefault('heat_grid_boost_max_outdoor_c', 10)
    return result


def tariff_window(config, now):
    """Konfigurierte günstige Fenster in Europe/Berlin; leer/ungültig sperrt.

    Zeitgrenzen sind lokale Wanduhrzeiten. Bei der Zeitumstellung umfasst ein
    Fenster beide wiederholten Stunden; eine fehlende Stunde verkürzt es.
    """
    result = {'valid': False, 'reason': 'tariff_unsupported', 'start_ts': None,
              'end_ts': None, 'price_ct': None, 'revision': None}
    if config.get('stromtarif_typ') != 'octopus_heat':
        return result
    prices = [number(config.get(k)) for k in ('strompreis_cheap', 'strompreis_basis', 'strompreis_uht')]
    if any(v is None for v in prices) or prices[0] < 0 or prices[0] >= min(prices[1:]):
        return {**result, 'reason': 'tariff_prices_invalid'}
    windows, invalid = parse_allowed_windows(migrate(config).get('heat_tariff_shift_windows'))
    if invalid or not windows:
        return {**result, 'reason': 'window_invalid' if invalid else 'window_empty',
                'hint': 'Erlaubte Zeitfenster fehlen oder sind ungültig. Bitte HH:MM-HH:MM je Zeile eintragen.'}
    local = datetime.fromtimestamp(now, TARIFF_TIMEZONE)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0, fold=0)
    spans = []
    for offset in (-1, 0):
        day = midnight + timedelta(days=offset)
        for lo, hi in windows:
            start = (day + timedelta(minutes=lo)).timestamp()
            end = (day + timedelta(minutes=hi + (1440 if hi < lo else 0))).timestamp()
            if start <= now < end:
                spans.append((start, end, lo))
    if spans:
        start, end, lo = max(spans, key=lambda span: span[1])
        material = {'tariff': 'octopus_heat', 'prices': prices, 'windows': windows,
                    'start_ts': start, 'end_ts': end}
        return {**result, 'valid': True, 'reason': 'cheap_tariff_window',
                'start_ts': start, 'end_ts': end, 'price_ct': prices[0],
                'night': lo < 720 or lo >= 1080, 'revision': revision(material)}
    return {**result, 'reason': 'outside_cheap_window'}


def forecast_contract(live, now, horizon_end, *, max_absorption_w=None):
    """Integriert kanonische Slotwerte lückenlos, ohne Lasten doppelt abzuziehen."""
    base = {'valid': False, 'reason': 'forecast_missing', 'need_kwh': None,
            'pv_cover_kwh': None, 'missing_kwh': None, 'pv_method': None, 'load_method': None,
            'revision': None, 'horizon_end_ts': horizon_end}
    if live.get('heat_forecast_source_reason'):
        return {**base, 'reason': live['heat_forecast_source_reason']}
    meta = live.get('storage_plan_meta') or {}
    projection = live.get('heat_price_boost_forecast') or {}
    if not isinstance(meta, dict) or not isinstance(projection, dict):
        return {**base, 'reason': 'forecast_structure_invalid'}
    if not meta.get('plan_id') or projection.get('plan_id') != meta.get('plan_id'):
        return {**base, 'reason': 'forecast_plan_binding_invalid'}
    def ts(value):
        raw = number(value)
        if raw is not None:
            return raw / 1000 if raw > 1e11 else raw
        try:
            return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
        except (TypeError, ValueError):
            return None
    generated = ts(meta.get('generated_at'))
    until = ts(meta.get('valid_until'))
    if generated is None or until is None or not 0 <= now - generated <= 1800 or now >= until + 300.0:
        return {**base, 'reason': 'forecast_stale'}
    revisions = meta.get('input_revisions') or {}
    if not isinstance(revisions, dict) or not all(revisions.get(k) for k in ('pv_ensemble', 'load_ensemble', 'config')):
        return {**base, 'reason': 'forecast_revision_missing'}
    if projection.get('input_revisions') != revisions:
        return {**base, 'reason': 'forecast_revision_mismatch'}
    cursor, need, cover = now, 0., 0.
    methods, load_methods = set(), set()
    from .plan_forecast import slot_forecast_values
    slots = projection.get('slots')
    if not isinstance(slots, list) or any(not isinstance(row, dict) for row in slots):
        return {**base, 'reason': 'forecast_slots_invalid'}
    rows = sorted(slots, key=lambda s: number(s.get('start_ts_ms')) or 0)
    for row in rows:
        raw_a, raw_b = number(row.get('start_ts_ms')), number(row.get('end_ts_ms'))
        a, b = (raw_a / 1000 if raw_a is not None else None), (raw_b / 1000 if raw_b is not None else None)
        if a is None or b is None or b <= a:
            return {**base, 'reason': 'forecast_slot_invalid'}
        if b <= now or a >= horizon_end:
            continue
        values = slot_forecast_values(row)
        if not values['valid']:
            return {**base, 'reason': values['reason']}
        if (max(a, now) != cursor or row.get('forecast_fresh') is not True
                or row.get('pv_forecast_fresh', True) is not True):
            return {**base, 'reason': 'forecast_gap_or_stale'}
        hp, house, wb = (values[k] for k in ('heat', 'house', 'wallbox'))
        pv, method = values['pv'], values['pv_method']
        load_methods.update(values['load_method'].split(' + '))
        hours = (min(b, horizon_end) - cursor) / 3600
        # Die kanonische house-Komponente ist die Grundlast ohne WP/Wallbox.
        # Keine zusätzlichen Mess-/Alt-/Reservierungswerte von dieser Quelle abziehen.
        need += hp * hours / 1000
        surplus = max(0., pv - house - wb)
        if max_absorption_w is not None:
            surplus = min(surplus, max_absorption_w)
        cover += surplus * hours / 1000
        methods.add(method)
        cursor = min(b, horizon_end)
        if cursor == horizon_end:
            break
    if cursor < horizon_end:
        return {**base, 'reason': 'forecast_horizon_incomplete'}
    return {**base, 'valid': True, 'reason': 'forecast_complete', 'need_kwh': need,
            'pv_cover_kwh': cover, 'missing_kwh': max(0., need - cover),
            'pv_method': ' + '.join(sorted(methods)),
            'load_method': ' + '.join(sorted(load_methods)),
            'revision': revision([meta['plan_id'], revisions, now, horizon_end, need, cover])}


def ww_duration(history, fallback_min):
    """Nur vollständig beobachtete WW-Läufe; oberes Quartil der letzten acht."""
    values = [number(v) for v in history[-8:]] if isinstance(history, list) else []
    values = sorted(v for v in values if v is not None and 60 <= v <= 4 * 3600)
    if len(values) >= 3:
        return values[math.ceil(.75 * len(values)) - 1], 'observed_ww_cycles'
    fallback = number(fallback_min)
    return (fallback * 60, 'configured_lead') if fallback is not None and fallback > 0 else (None, 'ww_duration_missing')


def decide(config, evidence, state, *, now):
    """Zentrale Entscheidung; der Dispatcher führt ausschließlich deren Intent aus."""
    cfg = migrate(config)
    old = copy.deepcopy(state or {})
    window = tariff_window(cfg, now)
    mode = cfg.get('heat_tariff_shift_mode')
    forecast = evidence.get('forecast') or {}
    minimum = number(cfg.get('price_min_duration', 60))
    maximum = number(cfg.get('price_max_daily', 180))
    consumed = number(old.get('used_s'))
    remaining = None if maximum is None or consumed is None else max(0., maximum * 60 - consumed)
    result = {'schema': 'heat_tariff_shift_v1', 'mode': mode, 'ts': now,
              'would_start': False, 'commands_allowed': False, 'blockers': [],
              'target': None, 'target_c': None, 'start_ts': None,
              'window': window, 'forecast': forecast, 'remaining_daily_s': remaining,
              'request_id': None, 'revision': 0, 'valid_until_ts': now + 30,
              'hold_w': None}
    blockers = result['blockers']
    gates = {
        'mode_off': mode not in ('shadow', 'active'),
        'device_unsupported': str(cfg.get('wp_type')) != '0',
        'auto_off': not enabled(cfg.get('auto_mode')),
        'heat_policy_off': not enabled(cfg.get('heat_policy_runtime_enable')),
        'window_or_price_invalid': not window['valid'],
        'forecast_invalid': forecast.get('valid') is not True,
        'daily_state_unknown': consumed is None,
        'duration_config_invalid': minimum is None or minimum < 10 or maximum is None or maximum <= 0,
    }
    for name in ('data_fresh', 'reserve_free', 'emergency_free', 'storage_free',
                 'device_free', 'source_free', 'holiday_free', 'restart_free', 'user_free'):
        gates[name + '_missing_or_blocked'] = evidence.get(name) is not True
    blockers.extend(k for k, blocked in gates.items() if blocked)
    residual = number(forecast.get('missing_kwh'))
    if residual is None:
        blockers.append('heat_need_unknown')
    elif residual <= 0:
        blockers.append('pv_covers_need' if number(forecast.get('need_kwh')) else 'no_heat_need')
    cap = number(evidence.get('power_limit_w'))
    if cap is None or cap <= 0:
        blockers.append('power_profile_missing')
    ww, hz = evidence.get('ww') or {}, evidence.get('hz') or {}
    scope = normalize_scope(cfg.get('heat_price_boost_scope', 'both'))
    if scope is None:
        blockers.append('scope_invalid')
    channels = [('ww', ww)] if scope == 'dhw' else [('hz', hz)] if scope == 'heating' else [('ww', ww), ('hz', hz)]
    for channel, data in channels:
        actual, target = number(data.get('actual_c')), number(data.get('target_c'))
        if actual is None or target is None or not 0 <= actual <= 100 or not 0 < target <= 100:
            blockers.append(channel + '_temperature_missing')
    duration, duration_source = ww_duration(old.get('ww_durations_s'), cfg.get('heat_tariff_shift_ww_lead_min'))
    result.update(ww_duration_s=duration, ww_duration_source=duration_source)
    ww_need = number(ww.get('actual_c')) is not None and number(ww.get('target_c')) is not None and ww['actual_c'] < ww['target_c']
    hz_need = number(hz.get('actual_c')) is not None and number(hz.get('target_c')) is not None and hz['actual_c'] < hz['target_c']
    ww_need = ww_need and scope in ('dhw', 'both')
    hz_need = hz_need and scope in ('heating', 'both')
    outdoor_blocked = hz_need and (evidence.get('summer') is not False or not heating_boost_allowed(
            cfg, evidence.get('outside_mean_c'),
            running=old.get('active_target') == 'hz' and bool(old.get('request_id'))))
    if outdoor_blocked:
        hz_need = False
    target = 'ww' if ww_need else 'hz' if hz_need else None
    result.update(target=target, target_c=(ww if target == 'ww' else hz).get('target_c') if target else None)
    if target is None:
        blockers.append('outdoor_temperature_limit' if outdoor_blocked else 'targets_reached')
    if duration is None:
        blockers.append('ww_duration_missing')
    if window['valid'] and duration is not None:
        reserve = 600.
        # Reserve wird von der Endzeit abgezogen, nicht danach addiert.
        result['latest_ww_start_ts'] = window['end_ts'] - duration - reserve
        result['start_ts'] = (max(window['start_ts'], result['latest_ww_start_ts']) if target == 'ww' else now)
        if target == 'ww' and hz_need and minimum is not None:
            result['start_ts'] = max(window['start_ts'], result['start_ts'] - minimum * 60)
        if now < result['start_ts']:
            blockers.append('planned_start_pending')
    running = old.get('active_target') == target and old.get('request_id')
    if target == 'ww' and not running and window['valid'] and duration is not None and now > window['end_ts'] - duration - 600:
        blockers.append('ww_duration_does_not_fit_window')
    if minimum is not None and minimum >= 10:
        run_s = max(600., minimum * 60, (number(cfg.get('wp_min_runtime_min', 30)) or 0.) * 60)
        if not running and window['valid'] and window['end_ts'] - now < minimum * 60:
            blockers.append('minimum_does_not_fit_window')
        if remaining is not None and remaining < (1 if running else run_s):
            blockers.append('daily_maximum')
        if cap and residual is not None and not running and residual * 3600000 / cap < run_s:
            blockers.append('remaining_energy_below_minimum')
    if evidence.get('higher_owner'):
        blockers.append('higher_priority_owner')
    if not blockers:
        result['would_start'] = True
        result['request_id'] = old.get('request_id') if running else 'tariff:' + revision([window['revision'], target, old.get('used_s')])[:40]
        result['commands_allowed'] = mode == 'active'
    result['intent_revision'] = revision({k: result[k] for k in ('request_id', 'target', 'target_c', 'window')})
    return result


def forecast_to_next_pv(live, now):
    """Bedarf bis zum nächsten nutzbaren PV-Abschnitt, maximal 24 Stunden.

    PV ist nutzbar, sobald nach Hausgrundlast und Wallbox ein Überschuss
    verbleibt. Dessen zusammenhängender Abschnitt deckt den zuvor erwarteten
    Wärmebedarf; die normale Komfortregelung wird dadurch nie gesperrt.
    Ohne nutzbaren Abschnitt bleibt der vollständige 24-Stunden-Restbedarf.
    """
    end = now + 86400
    result = forecast_contract(live, now, end)
    if not result['valid']:
        return result
    rows = (live.get('heat_price_boost_forecast') or {}).get('slots') or []
    next_pv, pv_end, cover = None, None, 0.
    from .plan_forecast import slot_forecast_values
    for row in sorted(rows, key=lambda row: number(row['start_ts_ms'])):
        a, b = max(now, number(row['start_ts_ms']) / 1000), min(end, number(row['end_ts_ms']) / 1000)
        if b <= a:
            continue
        values = slot_forecast_values(row)
        surplus = max(0., values['pv'] - values['house'] - values['wallbox'])
        if surplus > 0:
            if next_pv is None:
                next_pv = a
            cover += surplus * (b-a) / 3600000
            pv_end = b
        elif next_pv is not None:
            break
    if next_pv is None:
        return {**result, 'next_pv_ts': None, 'reason': 'no_usable_pv_within_24h'}
    need = 0.
    for row in rows:
        a, b = max(now, number(row['start_ts_ms']) / 1000), min(next_pv, number(row['end_ts_ms']) / 1000)
        if b > a:
            need += slot_forecast_values(row)['heat'] * (b-a) / 3600000
    return {**result, 'need_kwh': need, 'pv_cover_kwh': cover,
            'missing_kwh': max(0., need-cover), 'next_pv_ts': next_pv,
            'pv_period_end_ts': pv_end, 'horizon_end_ts': next_pv,
            'reason': 'need_until_next_usable_pv',
            'revision': revision([result['revision'], next_pv, pv_end, need, cover])}
