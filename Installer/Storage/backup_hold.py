#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reine Vorhalteempfehlung im Netzbetrieb; keine Gerätefreigabe, kein Konto."""
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import hashlib
import json
import math
import re

SCHEMA = 'storage_backup_hold_observation_v1'
KEYS = ('storage_backup_hold_enable', 'storage_backup_energy_kwh',
        'storage_backup_reference_time', 'storage_backup_until_time',
        'storage_backup_grid_charge_enable', 'storage_backup_grid_price_limit_ct')
POINT_FACTOR = 0.8  # Sichtbare Heuristik, keine kalibrierte Wahrscheinlichkeit.
SLOT_MS = 900000
LIVE_MAX_AGE_MS = 30000  # Dieselbe Messfrist für Erzeuger und Leser.


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(str(value).strip().replace(',', '.'))
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def digest(value):
    return 'sha256:' + hashlib.sha256(json.dumps(value, sort_keys=True,
        ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def config_contract(config):
    """Ungültige Eingaben betreffen ausschließlich die neue Beobachtung."""
    cfg = dict(config or {})
    def toggle(key):
        value = cfg.get(key, 0)
        text = str(value).strip().lower()
        return (text in ('1', 'true', 'yes', 'on'),
                text in ('0', '1', 'false', 'true', 'no', 'yes', 'off', 'on'))
    raw_mode = str(cfg.get(KEYS[0], 0)).strip().lower()
    enabled, enabled_valid = toggle(KEYS[0])
    mode = 1 if enabled else 0
    if raw_mode == '2':
        enabled, enabled_valid, mode = True, True, 2
    grid, grid_valid = toggle(KEYS[4])
    energy = number(cfg.get(KEYS[1]))
    reference = str(cfg.get(KEYS[2], '20:00')).strip()
    until = str(cfg.get(KEYS[3]) or '').strip()
    price = number(cfg.get(KEYS[5]))
    reasons = []
    if not enabled_valid or not grid_valid:
        reasons.append('backup_switch_invalid')
    if enabled:
        if energy is None or energy <= 0 or not math.isfinite(energy * 1000):
            reasons.append('backup_energy_missing_or_invalid')
        valid_time = lambda s: re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', s) is not None
        if not valid_time(reference) or not valid_time(until) or reference == until:
            reasons.append('backup_reference_times_missing_or_invalid')
        if grid and price is None:
            reasons.append('backup_grid_price_missing_or_invalid')
    values = dict(zip(KEYS, (2 if mode == 2 else enabled, energy, reference, until, grid, price if grid else None)))
    revision_material = values if mode != 2 else dict(values=values,
        tariff={k: v for k, v in cfg.items() if k.startswith(('strom', 'tariff_', 'octopus_'))})
    return dict(enabled=enabled, mode=mode, valid=not reasons, reasons=reasons,
                values=values, revision=digest(revision_material))


def local_boundary(day, clock, zone):
    """Fold: erster UTC-Zeitpunkt; Lücke: um die Sprungdauer vorwärts."""
    hour, minute = map(int, clock.split(':'))
    naive = datetime(day.year, day.month, day.day, hour, minute)
    variants = [naive.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
    real = [v for v in variants if datetime.fromtimestamp(v.timestamp(), zone).replace(tzinfo=None) == naive]
    if real:
        return min(v.timestamp() for v in real) * 1000
    later = [datetime.fromtimestamp(v.timestamp(), zone) for v in variants
             if datetime.fromtimestamp(v.timestamp(), zone).replace(tzinfo=None) > naive]
    return min(v.timestamp() for v in later) * 1000


def cycle(config, now_ms, zone):
    values = config['values']
    local = datetime.fromtimestamp(now_ms / 1000, zone)
    day = local.date()
    ref = values[KEYS[2]]
    if local_boundary(day, ref, zone) > now_ms:
        day -= timedelta(days=1)
    start = local_boundary(day, ref, zone)
    end = local_boundary(day + timedelta(days=1), ref, zone)
    until_day = day if values[KEYS[3]] > ref else day + timedelta(days=1)
    until = local_boundary(until_day, values[KEYS[3]], zone)
    return start, end, until


def unknown(config, reason, plan_id=None):
    return dict(schema_version=SCHEMA, observation_only=True, commands_allowed=False,
        enabled=config['enabled'], valid=False, reason=reason, plan_id=plan_id,
        config_revision=config['revision'], source_revision=None,
        generated_at_ts_ms=None, valid_until_ts_ms=None, source_age_s=None,
        calculated_energy_wh=None, calculated_at_ts_ms=None, current_energy_wh=None,
        live_source_age_s=None, capacity_wh=None, floor_wh=None, floor_soc_pct=None, evening_target_wh=None,
        evening_deficit_wh=None, pv_takeover_ts_ms=None, would_hold=None,
        remaining_energy_wh=None, recharge_pv_wh=None, curve=[],
        configured_energy_wh=None, reference_duration_h=None, takeover_energy_wh=None,
        physical_buffer_status='not_evaluated',
        earlier_takeover_semantics='shortens_task_without_rescaling_load',
        post_takeover_coverage=dict(status='not_checked', horizon_ts_ms=None, deficit_wh=None, deficits=[]),
        forecast_diagnostic=dict(mode='point_derating', formula='point_forecast_w * 0.8',
            quantile=None, convention='not_applicable_point_forecast', calibrated=False,
            source='pv_forecast.json', original_issue_ts_ms=None, target_horizon_ts_ms=None,
            quality_status='invalid'), change_reasons=[reason],
        grid_charge=dict(enabled=config['values'][KEYS[4]], required_kwh=None,
            candidates=[], selected=[], reason=reason))


def _issued_ms(meta, zone):
    issue = meta.get('forecast_issue_contract') or {}
    stamp = number(issue.get('producer_issued_at_utc_s'))
    if stamp is not None:
        return stamp * 1000
    try:
        value = datetime.fromisoformat(str(meta.get('fetched_at')))
        if value.tzinfo is None:
            value = value.replace(tzinfo=zone, fold=0)
        return value.timestamp() * 1000
    except (ValueError, TypeError, OverflowError):
        return None


def _axis(rows):
    result = {}
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        start, end = number(row.get('start_timestamp')), number(row.get('end_timestamp'))
        if start is None or end != start + SLOT_MS or start % SLOT_MS or start in result:
            return None
        result[int(start)] = row
    return result


def _takeover(pv, after_ms, last_ms, load_w):
    """Austauschbare Übernahmeheuristik gegen die konstante Notlast."""
    first = int(math.ceil(after_ms / SLOT_MS) * SLOT_MS)
    for start in range(first, int(last_ms) - 3 * SLOT_MS, SLOT_MS):
        if all(pv.get(start + j * SLOT_MS, -1) >= load_w for j in range(4)):
            return start
    return None


def _post_takeover(pv, candidate_ms, horizon_ms, load_w):
    """Chronologische Ersatzlastdefizite nach dem Kandidaten, keine Inselprüfung."""
    result = dict(status='no_candidate' if candidate_ms is None else 'checked',
        horizon_ts_ms=int(horizon_ms), deficit_wh=None, deficits=[])
    if candidate_ms is None:
        return result
    for ts in range(int(candidate_ms), int(horizon_ms), SLOT_MS):
        if ts not in pv:
            result.update(status='not_checked', deficit_wh=None)
            return result
        end = min(ts + SLOT_MS, horizon_ms)
        deficit = max(0, load_w - pv[ts]) * (end - ts) / 3600000
        if deficit > 0:
            result['deficits'].append(dict(start_ts_ms=ts, end_ts_ms=int(end), deficit_wh=round(deficit, 3)))
    result['deficit_wh'] = round(sum(row['deficit_wh'] for row in result['deficits']), 3)
    return result


def grid_diagnostic(config, plan, now_ms, deadline_ms, need_wh, charge_w, efficiency):
    values = config['values']
    result = dict(enabled=values[KEYS[4]], price_limit_ct=values[KEYS[5]],
        required_kwh=None if need_wh is None else round(need_wh / efficiency / 1000, 6),
        candidates=[], selected=[], missing_price_slots=0, reason='grid_charge_disabled',
        available_grid_kwh=None, remaining_deficit_wh=None, commands_allowed=False,
        horizon_complete=False, price_comparison='strictly_less',
        price_basis='full_import_price_ct_per_kwh', selection_order='price_then_time',
        energy_basis='grid_import_before_explicit_charge_efficiency')
    if not values[KEYS[4]]:
        result['required_kwh'] = None
        return result
    try:
        from ..tariff_schedule import (recurring_tariff_slots, recurring_tariff_stages,
            uses_recurring_tariff_axis, STATIC_TARIFF_TYPES, tariff_type)
    except ImportError:
        from tariff_schedule import (recurring_tariff_slots, recurring_tariff_stages,
            uses_recurring_tariff_axis, STATIC_TARIFF_TYPES, tariff_type)
    cfg = config['raw']
    if uses_recurring_tariff_axis(cfg):
        stages = recurring_tariff_stages(cfg)
        if (stages is not None and not stages['valid']) or (
                tariff_type(cfg) in STATIC_TARIFF_TYPES and number(cfg.get('strompreis_basis')) is None):
            result['reason'] = 'tariff_stages_missing_or_invalid'
            return result
        prices = [dict(row, fresh=True) for row in recurring_tariff_slots(
            cfg, now_ms=now_ms, lookback_ms=0, horizon_ms=max(SLOT_MS, deadline_ms - now_ms))]
    else:
        prices = [dict(start_timestamp=s['start_ts_ms'], end_timestamp=s['end_ts_ms'],
            billing_price_ct=(s.get('prices_ct_kwh') or {}).get('buy'),
            fresh=(s.get('prices_ct_kwh') or {}).get('fresh')) for s in plan.get('slots', [])]
    # Materialisiere die vollständige Viertelstundenachse. Fehlende Zeilen
    # bleiben ebenso Lücken wie vorhandene Zeilen ohne gültigen Preis.
    first = int(now_ms // SLOT_MS * SLOT_MS)
    for ts in range(first, int(deadline_ms), SLOT_MS):
        start, end = max(now_ms, ts), min(deadline_ms, ts + SLOT_MS)
        matches = [row for row in prices if row['start_timestamp'] <= start
                   and row['end_timestamp'] >= end]
        row = matches[0] if len(matches) == 1 else {}
        price = number(row.get('billing_price_ct'))
        if price is None or row.get('fresh') is not True:
            result['missing_price_slots'] += 1
            continue
        if price < values[KEYS[5]]:
            result['candidates'].append(dict(start_ts_ms=int(start), end_ts_ms=int(end), price_ct=price))
    result['horizon_complete'] = result['missing_price_slots'] == 0
    result['candidates'].sort(key=lambda r: (r['price_ct'], r['start_ts_ms']))
    need_grid_wh = None if need_wh is None else need_wh / efficiency
    remaining = need_grid_wh
    available = 0.0
    for row in result['candidates']:
        possible = charge_w * (row['end_ts_ms'] - row['start_ts_ms']) / 3600000
        available += possible
        if remaining is not None and remaining > 0:
            amount = min(remaining, possible)
            result['selected'].append(dict(row, grid_wh=round(amount, 3)))
            remaining -= amount
    result.update(available_grid_kwh=round(available / 1000, 6),
        remaining_deficit_wh=None if remaining is None else round(remaining * efficiency, 3),
        reason='price_gap' if result['missing_price_slots'] else (
            'recommendation_only' if result['candidates'] else 'no_price_below_limit'))
    return result


def _build_observation(config, plan, *, now_ms, pv_rows, pv_meta, load_rows,
                      load_generated_ms, timeline, chargeable, capacity_wh,
                      energy_wh, reserve_wh, charge_w, efficiency, live_generated_ms=None, live_valid=False,
                      timezone_name='Europe/Berlin'):
    """Ein eigenes gebundenes Top-Level-Feld nach dem kanonischen Planhash."""
    contract = config_contract(config)
    if not contract['enabled']:
        return None
    result = unknown(contract, ';'.join(contract['reasons']) or 'unknown', plan.get('plan_id'))
    if not contract['valid']:
        return _bind(result)
    try:
        zone = ZoneInfo(timezone_name)
        start, end, until = cycle(contract, now_ms, zone)
    except (ValueError, KeyError, OverflowError):
        result['reason'] = 'backup_timezone_invalid'
        return _bind(result)
    contract['raw'] = config
    values = contract['values']
    base_wh = values[KEYS[1]] * 1000
    result.update(generated_at_ts_ms=int(now_ms), valid_until_ts_ms=int(min(end, now_ms + 1800000)),
        cycle_start_ts_ms=int(start), cycle_end_ts_ms=int(end), reference_until_ts_ms=int(until),
        timezone=timezone_name, time_resolution='first_fold_shift_gap_forward',
        energy_basis='stored_energy_wh', normal_load_basis='existing_single_load_balance',
        pv_scope='grid_operation_heuristic', point_factor=POINT_FACTOR,
        probability=None, quantiles_used=False, charge_efficiency=efficiency, evening_target_wh=base_wh)
    duration_h = (until - start) / 3600000
    if duration_h <= 0:
        result['reason'] = 'backup_reference_duration_invalid'
        return _bind(result)
    load_w = base_wh / duration_h
    if not math.isfinite(load_w):
        result['reason'] = 'backup_energy_missing_or_invalid'
        return _bind(result)
    result.update(emergency_load_w=load_w, reference_duration_h=duration_h,
        configured_energy_wh=base_wh)
    if any(number(v) is None for v in (capacity_wh, energy_wh, reserve_wh, charge_w, efficiency)) or (
            capacity_wh <= 0 or not 0 < efficiency <= 1 or charge_w < 0 or not 0 <= energy_wh <= capacity_wh):
        result['reason'] = 'backup_capacity_energy_or_limits_unknown'
        return _bind(result)
    result.update(capacity_wh=capacity_wh, capacity_source='configured_or_live_usable', physical_reserve_wh=reserve_wh)
    live_stamp = number(live_generated_ms)
    result['live_source_age_s'] = None if live_stamp is None else (now_ms - live_stamp) / 1000
    if live_valid is not True or live_stamp is None or not 0 <= now_ms - live_stamp < LIVE_MAX_AGE_MS:
        result['reason'] = 'backup_live_unknown_or_stale'
        return _bind(result)

    result.update(calculated_energy_wh=energy_wh, calculated_at_ts_ms=int(now_ms),
        current_energy_wh=energy_wh)

    forecast = _axis(pv_rows)
    loads = _axis(load_rows)
    issued = _issued_ms(pv_meta, zone)
    result['source_age_s'] = None if issued is None else (now_ms - issued) / 1000
    # Ausgabezeit des Produzenten hat Vorrang vor der Verpackungszeit.
    result['forecast_diagnostic']['original_issue_ts_ms'] = issued
    def invalid(reason):
        result.update(reason=reason, floor_wh=max(reserve_wh, base_wh),
            floor_soc_pct=max(reserve_wh, base_wh) / capacity_wh * 100, would_hold=True)
        # Ohne PV-Beweis ist nur das bekannte Ziel als Auffüllbedarf nutzbar.
        result['grid_charge'] = grid_diagnostic(contract, plan, now_ms, end,
            max(0, min(capacity_wh, max(base_wh, reserve_wh)) - energy_wh), charge_w, efficiency)
        result['change_reasons'] = [reason]
        return _bind(result)
    try:
        result['source_revision'] = digest(dict(pv=pv_rows, meta=pv_meta, loads=load_rows,
            load_generated_ms=load_generated_ms, live_generated_ms=live_generated_ms, live_valid=live_valid,
            capacity_wh=capacity_wh, energy_wh=energy_wh, reserve_wh=reserve_wh,
            charge_w=charge_w, efficiency=efficiency, chargeable=chargeable, timeline=timeline))
    except (TypeError, ValueError, OverflowError):
        return invalid('forecast_source_revision_invalid')
    issue_id = (pv_meta.get('forecast_issue_contract') or {}).get('issue_id')
    if issue_id is not None and any(row.get('forecast_issue_id') != issue_id for row in pv_rows):
        return invalid('pv_forecast_issue_mismatch')
    if issued is None or not 0 <= now_ms - issued < 10800000:
        return invalid('pv_forecast_age_unknown_or_stale')
    if load_generated_ms is None or not 0 <= now_ms - load_generated_ms < 10800000:
        return invalid('load_forecast_age_unknown_or_stale')
    if not forecast or not loads:
        return invalid('forecast_missing_or_invalid')
    first = int(now_ms // SLOT_MS * SLOT_MS)
    # Laufender Tag plus die nächste Nacht; keine Tagesprofil-Ersatzprognose.
    horizon = local_boundary(datetime.fromtimestamp(end / 1000, zone).date() + timedelta(days=1),
                             values[KEYS[2]], zone)
    horizon = max(horizon, end + 4 * SLOT_MS)
    result['forecast_diagnostic']['target_horizon_ts_ms'] = int(horizon)
    result['post_takeover_coverage']['horizon_ts_ms'] = int(end)
    pv = {}; normal = {}; charges = {}
    normalized = {int(r['ts']): r for r in timeline}
    for ts in range(first, int(math.ceil(horizon / SLOT_MS) * SLOT_MS), SLOT_MS):
        row, ml = forecast.get(ts), loads.get(ts)
        if row is None or ml is None:
            return invalid('forecast_horizon_gap')
        p = number(row.get('predicted_kwh'))
        if p is None or p < 0 or row.get('pv_forecast_fresh', row.get('forecast_fresh')) is not True:
            return invalid('pv_forecast_value_or_provenance_invalid')
        amounts = [number(ml.get(k, 0 if k != 'home_kwh' else None)) for k in ('home_kwh', 'wp_kwh', 'climate_kwh')]
        if any(v is None or v < 0 for v in amounts):
            return invalid('load_forecast_value_invalid')
        pv[ts] = p * 1000 * POINT_FACTOR
        normalized_row = normalized.get(ts)
        bound = chargeable.get(ts)
        if normalized_row is None or bound is None or bound.get('complete') is not True:
            return invalid('chargeable_forecast_unbound')
        # Der Bestandsvertrag enthält Haus, WP, Klima und geplante Lasten genau einmal.
        normal[ts] = max(sum(amounts) * 1000, number(normalized_row.get('normal_load_w')) or 0)
        surplus = max(0, pv[ts] - normal[ts])
        source_cap = number(bound.get('chargeable_surplus_w'))
        if source_cap is None:
            return invalid('chargeable_forecast_unknown')
        dc = number(bound.get('e3dc_dc_pv_w'))
        if bound.get('source_scope') == 'E3DC_DC_ONLY':
            if dc is None:
                return invalid('chargeable_dc_forecast_unknown')
            pv[ts] = min(pv[ts], dc * POINT_FACTOR)
            surplus = min(surplus, dc * POINT_FACTOR)
        charges[ts] = min(charge_w, source_cap, surplus) * efficiency
    current_h = _takeover(pv, max(now_ms, start), end, load_w)
    next_h = _takeover(pv, end, horizon, load_w)
    target = max(reserve_wh, base_wh, 0 if next_h is None else load_w * (next_h - end) / 3600000)
    recharge = 0.0
    for ts, watts in charges.items():
        hours = max(0, (min(ts + SLOT_MS, end) - max(ts, now_ms)) / 3600000)
        recharge += watts * hours
    pv_potential = recharge
    recharge_if_hold = min(pv_potential, max(0, capacity_wh - energy_wh))
    reachable = min(capacity_wh, energy_wh + recharge_if_hold)
    deficit = max(0, target - reachable)
    curve = []
    points = sorted(set([int(now_ms), int(end)] + [ts for ts in pv if now_ms <= ts <= end]))
    for ts in points:
        future_charge = sum(w * max(0, (min(s + SLOT_MS, end) - max(s, ts)) / 3600000)
                            for s, w in charges.items())
        night = base_wh if current_h is None else (0.0 if ts >= current_h else
            load_w * max(0, current_h - ts) / 3600000)
        floor = max(reserve_wh, night, target - future_charge)
        curve.append(dict(ts_ms=ts, floor_wh=round(floor, 3), floor_soc_pct=round(floor / capacity_wh * 100, 4)))
    floor = curve[0]['floor_wh']
    remaining = max(0, min(energy_wh - floor, pv_potential, energy_wh + pv_potential - target))
    takeover_unknown = current_h is None or next_h is None
    if takeover_unknown or deficit > 0:
        remaining = 0.0
    remaining = math.floor(remaining * 1000) / 1000
    # Nur eine hypothetische Entnahme jetzt; keine kumulative Freigabe. Sie
    # schafft den Laderaum für ihre eigene einmalige PV-Wiederaufladung.
    recharge = min(pv_potential, max(0, capacity_wh - energy_wh + remaining))
    result.update(valid=True, reason='target_exceeds_capacity' if target > capacity_wh else (
        'pv_takeover_unknown' if takeover_unknown else 'recommendation_only'),
        curve=curve, floor_wh=floor, floor_soc_pct=curve[0]['floor_soc_pct'],
        evening_target_wh=round(target, 3), evening_target_soc_pct=round(target / capacity_wh * 100, 4),
        evening_deficit_wh=round(deficit, 3), capacity_deficit_wh=round(max(0, target - capacity_wh), 3),
        reachable_evening_wh=round(reachable, 3), pv_takeover_ts_ms=current_h,
        next_pv_takeover_ts_ms=next_h, recharge_pv_wh=round(recharge, 3),
        remaining_energy_wh=round(remaining, 3), would_hold=remaining <= 0,
        recharge_if_hold_wh=round(recharge_if_hold, 3), pv_charge_potential_wh=round(pv_potential, 3),
        projected_evening_wh=round(min(capacity_wh, energy_wh - remaining + recharge), 3),
        recharge_assumption='candidate_discharge_now_then_hold',
        grid_charge=grid_diagnostic(contract, plan, now_ms, end,
            min(deficit, max(0, capacity_wh - reachable)), charge_w, efficiency))
    if contract['mode'] == 2:
        # Eigener Ausführungsvertrag: Beobachtung bleibt ausdrücklich nicht ausführbar.
        axis = [dict(ts_ms=ts, end_ts_ms=ts + SLOT_MS, pv_w=pv[ts],
                     charge_w=charges[ts], normal_load_w=normal[ts])
                for ts in sorted(pv) if ts < end]
        execution = dict(schema='storage_backup_execution_v1', commands_allowed=True,
            execution_owner='storage_manager',
            plan_id=plan.get('plan_id'), config_revision=contract['revision'],
            generated_at_ts_ms=int(now_ms), valid_until_ts_ms=int(min(end, now_ms + 1800000)),
            cycle_start_ts_ms=int(start), deadline_ts_ms=int(end),
            source_issue_ts_ms=issued, load_issue_ts_ms=load_generated_ms,
            forecast_identity=digest(dict(issue_id=issue_id, pv=pv_rows)),
            capacity_wh=capacity_wh, reserve_wh=reserve_wh, target_wh=target,
            emergency_load_w=load_w, takeover_ts_ms=current_h, axis=axis,
            charge_w=charge_w, efficiency=efficiency,
            source_scope='E3DC_DC_ONLY' if any(v.get('source_scope') == 'E3DC_DC_ONLY'
                for v in chargeable.values()) else 'TOTAL_PV')
        execution['binding'] = digest(execution)
        result['execution'] = execution
    result['grid_charge']['unfillable_capacity_wh'] = round(max(0, target - capacity_wh), 3)
    result['forecast_diagnostic']['quality_status'] = 'valid'
    result['takeover_energy_wh'] = None if current_h is None else round(load_w * (current_h - start) / 3600000, 3)
    result['post_takeover_coverage'] = _post_takeover(pv, current_h, end, load_w)
    # Diese Codes erklären die Empfehlung beim Vergleich von Beobachtungen;
    # kein persistentes Entladekonto oder Geräteereignisprotokoll.
    result['change_reasons'] = [result['reason'],
        'pv_takeover_unknown' if current_h is None else 'pv_takeover_candidate_found']
    if (result['post_takeover_coverage']['deficit_wh'] or 0) > 0:
        result['change_reasons'].append('later_emergency_deficit')
    if (result['grid_charge']['required_kwh'] or 0) > 0:
        result['change_reasons'].append('grid_residual_deficit')
    if result['grid_charge'].get('missing_price_slots'):
        result['change_reasons'].append('grid_price_gap')
    return _bind(result)


def build_observation(config, plan, **sources):
    """Auch eine unbrauchbare Zusatzrechnung lässt den kanonischen Plan bestehen."""
    try:
        return _build_observation(config, plan, **sources)
    except (ArithmeticError, ValueError, TypeError, KeyError):
        contract = config_contract(config)
        result = unknown(contract, 'backup_calculation_invalid', plan.get('plan_id'))
        now = number(sources.get('now_ms'))
        if now is not None:
            result.update(generated_at_ts_ms=int(now), valid_until_ts_ms=int(now + 1800000))
        return _bind(result)


def _bind(observation):
    if not observation['valid']:
        observation['change_reasons'] = [observation['reason']]
    observation['binding'] = digest({k: v for k, v in observation.items() if k != 'binding'})
    return observation


def read_observation(plan, config, now_ms, *, current_live=None):
    """Zusatzbindung kann ausschließlich die Diagnose ungültig machen."""
    contract = config_contract(config)
    fallback = unknown(contract, 'backup_disabled' if not contract['enabled'] else 'backup_plan_missing', plan.get('plan_id'))
    if not contract['enabled']:
        return fallback
    if not contract['valid']:
        fallback['reason'] = ';'.join(contract['reasons'])
        return fallback
    raw = plan.get('backup_hold_observation')
    if not isinstance(raw, Mapping):
        return fallback
    try:
        # Validierte Plansnapshots liefern Kopien ihrer verschachtelten Werte.
        data = dict(raw)
        valid = (data.get('schema_version') == SCHEMA and data.get('observation_only') is True
            and data.get('commands_allowed') is False and data.get('plan_id') == plan.get('plan_id')
            and data.get('config_revision') == contract['revision']
            and data.get('binding') == digest({k: v for k, v in data.items() if k != 'binding'}))
        generated, until = number(data.get('generated_at_ts_ms')), number(data.get('valid_until_ts_ms'))
        valid = valid and generated is not None and until is not None and generated <= now_ms < until
        if valid:
            source_age = number(data.get('source_age_s'))
            current_age = None if source_age is None else source_age + (now_ms - generated) / 1000
            data['source_current_age_s'] = current_age
            data['read_at_ts_ms'] = int(now_ms)
            # Ursprünglicher Messbezug zählt weiter, auch bei frischen Folgemessungen.
            live_age = number(data.get('live_source_age_s'))
            data['live_source_age_s'] = None if live_age is None else live_age + (now_ms - generated) / 1000
            live = current_live if isinstance(current_live, Mapping) else {}
            stamp, energy = number(live.get('measured_at_ts_ms')), number(live.get('energy_wh'))
            if (live.get('valid') is not True or stamp is None or stamp <= 0 or energy is None
                    or energy < 0 or not 0 <= now_ms - stamp < LIVE_MAX_AGE_MS):
                data.update(valid=False, reason='backup_live_unknown_or_stale')
                # Speicherbezogene Empfehlungen sind bei unbekanntem Live-Stand unbekannt.
                # Der Halteboden ist rechnerisch unabhängig, bleibt hier aber ebenfalls unbekannt.
                for key in ('remaining_energy_wh', 'evening_deficit_wh', 'reachable_evening_wh',
                        'recharge_if_hold_wh', 'recharge_pv_wh', 'projected_evening_wh',
                        'floor_wh', 'floor_soc_pct', 'would_hold', 'curve',
                        'calculated_energy_wh', 'current_energy_wh'):
                    data[key] = None
                grid = dict(data.get('grid_charge') or {})
                for key in ('required_kwh', 'remaining_deficit_wh', 'available_grid_kwh', 'candidates', 'selected'):
                    grid[key] = None
                grid['reason'] = data['reason']
                data['grid_charge'] = grid
                data['change_reasons'] = list(dict.fromkeys(list(data.get('change_reasons') or []) + [data['reason']]))
                return data
            data['current_energy_wh'] = energy
            if current_age is not None and current_age >= 10800:
                data.update(valid=False, reason='pv_forecast_age_unknown_or_stale',
                    remaining_energy_wh=None, recharge_pv_wh=None, evening_deficit_wh=None)
                data['forecast_diagnostic'] = dict(data.get('forecast_diagnostic') or {}, quality_status='invalid')
                data['change_reasons'] = [data['reason']]
            return data
    except (TypeError, ValueError, OverflowError):
        pass
    fallback['reason'] = 'backup_binding_config_or_age_invalid'
    return fallback


REASON_TEXT = {'backup_live_unknown_or_stale': 'Speichermessung unbekannt oder zu alt', 'backup_disabled': 'Beobachtung ausgeschaltet', 'backup_plan_missing': 'Speicherplan fehlt', 'backup_binding_config_or_age_invalid': 'Planbindung, Konfiguration oder Planalter ungültig', 'backup_switch_invalid': 'Schalterwert ungültig', 'backup_energy_missing_or_invalid': 'Speicherinhalt fehlt oder ist ungültig', 'backup_reference_times_missing_or_invalid': 'Bestätigte Bezugszeiten fehlen oder sind ungültig', 'backup_grid_price_missing_or_invalid': 'Explizite Reservepreisgrenze fehlt oder ist ungültig', 'backup_timezone_invalid': 'Ortszeit ungültig', 'backup_reference_duration_invalid': 'Bezugsdauer nach Sommerzeitauflösung ungültig', 'backup_capacity_energy_or_limits_unknown': 'Kapazität, Speicherinhalt, Reserve oder Ladegrenze unbekannt', 'backup_calculation_invalid': 'Zusatzrechnung mit diesen Daten nicht möglich', 'pv_forecast_age_unknown_or_stale': 'PV-Prognosealter unbekannt oder zu alt', 'load_forecast_age_unknown_or_stale': 'Lastprognosealter unbekannt oder zu alt', 'forecast_missing_or_invalid': 'Prognose fehlt oder ist ungültig', 'forecast_horizon_gap': 'Prognosehorizont lückenhaft', 'pv_forecast_value_or_provenance_invalid': 'PV-Wert oder Quellenfrische ungültig', 'load_forecast_value_invalid': 'Lastprognosewert ungültig', 'forecast_source_revision_invalid': 'Quellenrevision ungültig', 'pv_forecast_issue_mismatch': 'PV-Prognose und Metadaten passen nicht zusammen', 'chargeable_forecast_unbound': 'Ladbarer PV-Anteil nicht gebunden', 'chargeable_forecast_unknown': 'Ladbarer PV-Anteil unbekannt', 'chargeable_dc_forecast_unknown': 'Ladbarer DC-Anteil unbekannt', 'pv_takeover_unknown': 'PV-Übernahme unbekannt', 'recommendation_only': 'Berechnete Empfehlung ohne Gerätewirkung', 'target_exceeds_capacity': 'Bedarf übersteigt Kapazität', 'backup_observation_stale_or_unknown': 'Beobachtung fehlt oder ist zu alt'}

def reason_text(code):
    return "; ".join(REASON_TEXT.get(part, "Beobachtungsdaten unbekannt") for part in str(code).split(";"))


def read_execution(plan, config, now_ms):
    """Separate Ausführungsbindung; niemals commands_allowed der Beobachtung umdeuten."""
    contract = config_contract(config)
    if contract['mode'] != 2 or not contract['valid']:
        return None
    observation = plan.get('backup_hold_observation') or {}
    if not isinstance(observation, Mapping):
        return None
    value = observation.get('execution')
    if not isinstance(value, Mapping):
        return None
    try:
        data = dict(value)
        if (data.get('schema') != 'storage_backup_execution_v1'
                or data.get('execution_owner') != 'storage_manager'
                or data.get('commands_allowed') is not True
                or data.get('plan_id') != plan.get('plan_id')
                or data.get('config_revision') != contract['revision']
                or data.get('binding') != digest({k: v for k, v in data.items() if k != 'binding'})
                or not data['generated_at_ts_ms'] <= now_ms < data['valid_until_ts_ms']
                or not 0 <= now_ms - data['source_issue_ts_ms'] < 10800000
                or not 0 <= now_ms - data['load_issue_ts_ms'] < 10800000
                or not data['generated_at_ts_ms'] < data['deadline_ts_ms']):
            return None
        fields = ('capacity_wh', 'reserve_wh', 'target_wh', 'emergency_load_w', 'charge_w', 'efficiency')
        if (any(type(data.get(k)) not in (int, float) or not math.isfinite(data[k]) for k in fields)
                or data['capacity_wh'] <= 0 or min(data['reserve_wh'], data['target_wh'], data['charge_w']) < 0
                or data['emergency_load_w'] <= 0 or not 0 < data['efficiency'] <= 1
                or data.get('source_scope') not in ('TOTAL_PV', 'E3DC_DC_ONLY')):
            return None
        axis = data.get('axis')
        if not isinstance(axis, list) or not axis:
            return None
        last_end = None
        for row in axis:
            if not isinstance(row, Mapping) or any(type(row.get(k)) not in (int, float)
                    or not math.isfinite(row[k]) for k in ('ts_ms','end_ts_ms','pv_w','charge_w','normal_load_w')):
                return None
            if (row['end_ts_ms'] != row['ts_ms'] + SLOT_MS or row['ts_ms'] % SLOT_MS
                    or min(row['pv_w'], row['charge_w'], row['normal_load_w']) < 0
                    or (last_end is not None and row['ts_ms'] != last_end)):
                return None
            last_end = row['end_ts_ms']
        if axis[0]['ts_ms'] > now_ms or last_end < data['deadline_ts_ms']:
            return None
        return data
    except (ArithmeticError, KeyError, TypeError, ValueError):
        return None


def execution_curve(execution, now_ms, reserve_wh):
    """Rückwärtsbilanz mit Sättigungsgrenze, Ladelimit und späteren Wolkenlücken.

    Ersatzlast und Hausverbrauch sind alternative Modelle. Die Ersatzlast
    wird nie als weiterer Verbraucher vom normalen Überschuss abgezogen.
    """
    end = execution['deadline_ts_ms']
    takeover = execution['takeover_ts_ms']
    load = execution['emergency_load_w']
    target = max(reserve_wh, execution['target_wh'])
    required = target
    curve = [dict(ts_ms=end, floor_wh=required)]
    for row in reversed(execution['axis']):
        ts, finish = max(now_ms, row['ts_ms']), min(end, row['end_ts_ms'])
        if finish <= ts:
            continue
        # Nach Übernahme bleibt ein späteres Lastloch eine echte Pflicht.
        # Vorher ist der unverändert kalibrierte Nachtbedarf maßgeblich.
        gap_w = max(0, load - row['pv_w']) if takeover is not None and ts >= takeover else 0
        required = max(reserve_wh, required + (gap_w - row['charge_w']) * (finish - ts) / 3600000)
        night = execution['target_wh'] if takeover is None else load * max(0, takeover - ts) / 3600000
        required = max(required, reserve_wh, night)
        curve.append(dict(ts_ms=ts, floor_wh=required))
    return list(reversed(curve))


def curve_value(curve, now_ms):
    if not curve:
        return None
    for left, right in zip(curve, curve[1:]):
        if left['ts_ms'] <= now_ms < right['ts_ms']:
            fraction = (now_ms - left['ts_ms']) / (right['ts_ms'] - left['ts_ms'])
            return left['floor_wh'] + fraction * (right['floor_wh'] - left['floor_wh'])
    if now_ms == curve[-1]['ts_ms']:
        return curve[-1]['floor_wh']
    return None


def control_context(config, plan, *, now_s, monotonic_s, live, reserve_wh,
                    capacity_wh, max_discharge_w, previous=None):
    """Pro Zyklus ein Wh-Rahmen aus aktueller Energie, ohne erneuerbares Guthaben."""
    contract = config_contract(config)
    if contract['mode'] != 2:
        return None
    previous = previous if isinstance(previous, Mapping) else {}
    now_ms = now_s * 1000
    stamp = number(live.get('_ts'))
    soc = number(live.get('SOC'))
    valid = (live.get('RSCP_Sample_Valid') is True and live.get('Power_Decision_Usable') is True
        and stamp is not None and 0 <= now_s - stamp < 30 and soc is not None and 0 <= soc <= 100
        and capacity_wh is not None and capacity_wh > 0 and reserve_wh is not None)
    result = dict(schema='storage_backup_control_v1', mode=2, active=False,
        valid=bool(valid), reason='backup_live_unknown_or_stale',
        measured_at_s=stamp, now_s=now_s, monotonic_s=monotonic_s,
        current_energy_wh=None, floor_wh=None, physical_reserve_wh=reserve_wh,
        discharge_cap_w=None, requested_discharge_w=None, confirmed_discharge_w=None,
        measured_battery_w=number(live.get('Battery_Power')),
        release_confirmed=False, deficit_wh=None, remaining_energy_wh=None,
        grid_required_wh=None, grid_reason='backup_live_unknown_or_stale', grid_job=None,
        price_limit_ct=contract['values'][KEYS[5]],
        locked=True, coverage_since_mono=None, takeover_since_mono=None,
        source_confirmation_since_mono=None, source_identities=[], approved_curve=[],
        closed_account=dict(previous.get('closed_account') or {}))
    if not valid:
        return result
    energy = capacity_wh * soc / 100
    full = max(reserve_wh, (contract['values'][KEYS[1]] or 0) * 1000)
    execution = read_execution(plan, config, now_ms)
    account_energy = energy
    account = dict(previous.get('closed_account') or {})
    account_key = digest(dict(capacity_wh=capacity_wh,
        energy_kwh=contract['values'][KEYS[1]], reference=contract['values'][KEYS[2]],
        until=contract['values'][KEYS[3]]))
    try:
        account_valid = (execution is not None and account.get('key') == account_key
            and account.get('deadline_ts_ms') == execution['deadline_ts_ms']
            and account.get('binding') == digest({k:v for k,v in account.items() if k != 'binding'})
            and number(account.get('energy_wh')) is not None and number(account.get('soc_energy_wh')) is not None
            and number(account.get('measured_at_s')) is not None and 0 <= now_s - account['measured_at_s']
            and 0 <= account['energy_wh'] <= capacity_wh)
    except (TypeError, ValueError):
        account_valid = False
    if account_valid:
        # Wiederaufnahme über einen Replan/Prozesswechsel: gemessener Bestand
        # ändert sich vor der einmaligen Anrechnung bereits bestätigter Ladung.
        accounted = account['energy_wh']
        if energy < account['soc_energy_wh']:
            accounted = min(accounted, energy)
        account_energy = max(energy, accounted)
        seconds = stamp - account['measured_at_s']
        battery_now = number(live.get('Battery_Power'))
        if 0 < seconds <= 5 and battery_now is not None:
            account_energy = max(energy, min(capacity_wh, accounted + battery_now * seconds / 3600))
    confirmed_takeover = number(account.get('confirmed_takeover_ts_ms')) if account_valid else None
    if execution:
        account = dict(key=account_key, deadline_ts_ms=execution['deadline_ts_ms'],
            energy_wh=account_energy, soc_energy_wh=energy, measured_at_s=stamp)
        if confirmed_takeover is not None:
            account['confirmed_takeover_ts_ms'] = confirmed_takeover
        account['binding'] = digest(account)
        result['closed_account'] = account
    result['accounted_energy_wh'] = account_energy
    result.update(active=True, current_energy_wh=energy, floor_wh=full,
        reason='backup_forecast_missing_full_hold', grid_reason='backup_forecast_missing_full_hold',
        energy_quantum_wh=capacity_wh / 100)
    if not contract['valid']:
        result['reason'] = ';'.join(contract['reasons'])
    last_stamp = number(previous.get('measured_at_s'))
    last_mono = number(previous.get('monotonic_s'))
    # Wiederholte/ältere Proben verdienen keine Dauer; eine Messlücke schließt sofort.
    continuous = bool(previous.get('valid') and stamp is not None and last_stamp is not None
        and 0 < stamp - last_stamp <= 5 and last_mono is not None and 0 < monotonic_s - last_mono <= 5
        and 0 < now_s - (number(previous.get('now_s')) or now_s) <= 5)
    reaction_s = 35.0
    buffer = capacity_wh / 100 + max_discharge_w * reaction_s / 3600
    hysteresis = max(capacity_wh / 100, max_discharge_w * reaction_s / 3600, 50)
    result.update(reaction_s=reaction_s, entry_buffer_wh=buffer, hysteresis_wh=hysteresis)
    # Geschlossenes Wiederaufladekonto. Ausschließlich beobachtete PV-Ladung
    # verdient neue Verkaufs-/Pre-Dump-Wh; Netzladung und ein Replan niemals.
    # Nach Neustart/Messlücke beginnt es geschlossen, nicht mit Plan-Rest-Wh.
    credit = max(0, number(previous.get('pv_recharge_credit_wh')) or 0) if continuous else 0
    battery = number(live.get('Battery_Power'))
    grid = number(live.get('Grid_Power'))
    if continuous and battery is not None and grid is not None:
        seconds = min(stamp - last_stamp, monotonic_s - last_mono)
        net_pv_charge = max(0, battery - max(0, grid))
        if execution and execution.get('source_scope') == 'E3DC_DC_ONLY':
            net_pv_charge = 0  # Ohne belegten DC-Istpfad kein zusätzliches Guthaben.
        credit = max(0, credit + (net_pv_charge - max(0, -battery)) * seconds / 3600)
    result['pv_recharge_credit_wh'] = min(credit, max(0, energy - reserve_wh))
    floor = full
    if not execution and contract['valid']:
        try:
            _, fallback_end, _ = cycle(contract, now_ms, ZoneInfo(str(plan.get('timezone') or 'Europe/Berlin')))
            result.update(approved_curve=[dict(ts_ms=now_ms, floor_wh=full),
                dict(ts_ms=fallback_end, floor_wh=full)], approved_identity='fallback')
        except (ValueError, KeyError, OverflowError):
            pass
    if execution:
        confirmed_takeover = number(account.get('confirmed_takeover_ts_ms')) if account_valid else None
        if confirmed_takeover is not None and confirmed_takeover <= now_ms:
            execution = dict(execution, takeover_ts_ms=confirmed_takeover)
        curve = execution_curve(execution, now_ms, reserve_wh)
        candidate = curve_value(curve, now_ms)
        old_curve = previous.get('approved_curve') or []
        old_floor = curve_value(old_curve, now_ms)
        identity = execution['forecast_identity']
        ids = list(previous.get('source_identities') or [])
        issue = execution['source_issue_ts_ms']
        if not ids or (ids[-1]['identity'] != identity and issue > ids[-1]['issue_ts_ms']):
            ids = (ids + [dict(identity=identity, issue_ts_ms=issue)])[-2:]
        stable_since = previous.get('source_confirmation_since_mono') if continuous else None
        candidate_ids = list(previous.get('candidate_source_identities') or []) if continuous else []
        if candidate is not None:
            floor = max(reserve_wh, candidate)
            if any(row['floor_wh'] > capacity_wh for row in curve):
                floor = max(floor, full)
                result['chronological_capacity_deficit_wh'] = max(row['floor_wh'] for row in curve) - capacity_wh
            if old_floor is not None and floor < old_floor - 0.001 and identity != previous.get('approved_identity'):
                stable_since = monotonic_s if stable_since is None else stable_since
                if not candidate_ids or (identity != candidate_ids[-1]['identity']
                        and issue > candidate_ids[-1]['issue_ts_ms']):
                    candidate_ids = (candidate_ids + [dict(identity=identity, issue_ts_ms=issue)])[-2:]
                if len(candidate_ids) < 2 or monotonic_s - stable_since < 300:
                    floor = max(floor, old_floor)
                    curve = [dict(ts_ms=r['ts_ms'], floor_wh=max(r['floor_wh'],
                        curve_value(old_curve, r['ts_ms']) or r['floor_wh'])) for r in curve]
                else:
                    stable_since = None
            else:
                stable_since = None
                candidate_ids = []
            takeover = execution['takeover_ts_ms']
            same_cycle = previous.get('deadline_ts_ms') == execution['deadline_ts_ms']
            prior_takeover = number(previous.get('takeover_ts_ms'))
            current_ts = int(now_ms // SLOT_MS * SLOT_MS)
            pv_axis = {r['ts_ms']: r['pv_w'] for r in execution['axis']}
            now_covered = all(pv_axis.get(current_ts+j*SLOT_MS, -1) >= execution['emergency_load_w'] for j in range(4))
            if (continuous and same_cycle and prior_takeover is not None
                    and prior_takeover <= now_ms and now_covered):
                takeover = min(prior_takeover, takeover) if takeover is not None else prior_takeover
            result['takeover_confirmed'] = bool(continuous and same_cycle and previous.get('takeover_confirmed'))
            result.update(reason='backup_hold', execution_binding=execution['binding'],
                deadline_ts_ms=execution['deadline_ts_ms'], takeover_ts_ms=takeover,
                approved_curve=curve, approved_identity=identity if stable_since is None else previous.get('approved_identity'),
                source_identities=ids, candidate_source_identities=candidate_ids,
                source_confirmation_since_mono=stable_since)
            # Vier prognostizierte Slots allein öffnen keinen Morgenboden.
            if takeover is not None and now_ms >= takeover:
                pv, home = number(live.get('PV_Power')), number(live.get('Home_Power'))
                covered = (pv is not None and home is not None and pv >= home
                    and live.get('Home_Power_Valid', True) is True)
                since = previous.get('takeover_since_mono') if continuous and covered else None
                since = monotonic_s if covered and since is None else since
                result['takeover_since_mono'] = since
                same_cycle = previous.get('deadline_ts_ms') == execution['deadline_ts_ms']
                confirmed = confirmed_takeover is not None or (same_cycle and previous.get('takeover_confirmed') is True)
                if not confirmed and (since is None or monotonic_s - since < 120):
                    floor = max(floor, number(previous.get('floor_wh')) or full)
                else:
                    result['takeover_confirmed'] = True
                    account['confirmed_takeover_ts_ms'] = takeover
                    account['binding'] = digest({k:v for k,v in account.items() if k != 'binding'})
                    result['closed_account'] = account
            # N7 nur aus frischem Bestand und noch zukünftiger, gebundener PV.
            # Keine Addition von Netzladung zum Wiederaufladebudget.
            potential = sum(r['charge_w'] * max(0, min(r['end_ts_ms'],execution['deadline_ts_ms'])
                - max(now_ms,r['ts_ms'])) / 3600000 for r in execution['axis'])
            # Der chronologische Boden enthält die späteren Lastlöcher bereits.
            need = max(0, candidate - account_energy)
            residual = min(max(0, capacity_wh - account_energy),
                max(need, execution['target_wh'] - min(capacity_wh, account_energy + potential), 0))
            result.update(grid_required_wh=residual, grid_reason='grid_charge_disabled')
            contract['raw'] = config
            diagnostic = grid_diagnostic(contract, plan, now_ms, execution['deadline_ts_ms'],
                residual, execution['charge_w'], execution['efficiency'])
            result['grid_reason'] = diagnostic['reason']
            current = next((r for r in diagnostic['selected'] if r['start_ts_ms'] <= now_ms < r['end_ts_ms']),None)
            if stable_since is not None:
                result['grid_reason'] = 'backup_forecast_change_pending'
                current = None
            if current and residual > 0:
                result['grid_job'] = dict(current, remaining_storage_wh=residual,
                    efficiency=execution['efficiency'], deadline_ts_ms=execution['deadline_ts_ms'],
                    target_energy_wh=min(capacity_wh, account_energy + residual),
                    execution_binding=execution['binding'], source_scope=execution['source_scope'])
    result['floor_wh'] = max(reserve_wh, floor)
    result['deficit_wh'] = max(0, result['floor_wh'] - energy)
    safe_wh = max(0, energy - result['floor_wh'] - buffer)
    coverage = safe_wh >= hysteresis
    since = previous.get('coverage_since_mono') if continuous and coverage else None
    since = monotonic_s if coverage and since is None else since
    locked = previous.get('locked', True)
    if energy <= result['floor_wh'] + buffer or not continuous:
        locked = True
    if coverage and since is not None and monotonic_s - since >= 120:
        locked = False
    result['export_cap_w'] = int(min(max_discharge_w, result['pv_recharge_credit_wh'] * 3600 / reaction_s))
    result.update(locked=bool(locked), coverage_since_mono=since,
        remaining_energy_wh=safe_wh,
        discharge_cap_w=0 if locked else int(min(max_discharge_w, safe_wh * 3600 / reaction_s)))
    return result
