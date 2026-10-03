"""Speichervertrag des Tariffenster-Auftrags; keine Lastreservierung."""
import copy
import math
from .tariff_shift import number, enabled, tariff_window


def apply(config, live, payload, request, *, now, reserve_pct, previous_state=None,
          min_interval_s=30.0, deadband_w=200):
    result = copy.deepcopy(payload)
    if config.get('heat_tariff_shift_mode', 'off') not in ('shadow', 'active'):
        return result
    request = request if isinstance(request, dict) else {}
    ts = number(live.get('_ts'))
    soc, reserve = number(live.get('SOC')), number(reserve_pct)
    grid = number(live.get('Grid_Power'))
    battery = number(live.get('Battery_Power', live.get('Bat_Power')))
    peak = result.get('peak_shaving')
    peak = peak if isinstance(peak, dict) else {}
    if isinstance(peak.get('enabled'), bool):
        peak_enabled = peak.get('enabled') is True
    else:
        # Dieselben Ein-Schreibweisen wie cfg_bool im Speicherregler.
        # Bei unbekanntem Schalter darf der Tarif-Halt die Kappung nicht verdrängen.
        raw = config.get('peak_shaving_enable')
        text = str(raw).strip().lower()
        peak_enabled = (raw if isinstance(raw, bool) else
                        text in ('1', 'true', 'yes', 'on', 'ja', 'ein')
                        or text not in ('0', 'false', 'no', 'off', 'nein', 'aus'))
    power = number(config.get('wp_pv_max_power_w'))
    peak_owner = result.get('priority') == 'peak_shaving' and result.get('protected') is True
    safe = bool(ts is not None and 0 <= now - ts <= 10 and live.get('RSCP_Sample_Valid') is True
                and soc is not None and reserve is not None and soc > reserve
                and grid is not None and battery is not None
                and power is not None and power > 0
                and live.get('Notstrom_Status', live.get('ems_emergency_power_status')) in (0, 2, 3)
                and not result.get('safety_veto') and not result.get('suppress_rscp_output')
                and not result.get('heatpump_pv_set_power_only') and not result.get('wallbox_fixed_start_set_power_only')
                and result.get('mode') == 0 and (not result.get('protected') or peak_owner)
                and not result.get('hard_mode_guard_errors')
                and enabled(config.get('auto_mode')) and str(config.get('wp_type')) == '0'
                and enabled(config.get('heat_policy_runtime_enable')))
    contract = {'ts': now, 'safety_free': safe, 'intent_revision': request.get('intent_revision'),
                'hold_w': None, 'peak_priority_w': None, 'active': False, 'reason': 'safety_or_source_blocked' if not safe else 'no_active_request'}
    contract['safety_veto'] = bool(result.get('safety_veto') or result.get('hard_mode_guard_errors'))
    result['heat_tariff_shift'] = contract
    if safe and peak_owner:
        # Die Kappung besitzt den Akku bereits. Nur der zusätzliche Tarif-Halt
        # entfällt; der laufende Wärmeauftrag und der Speicherrahmen bleiben.
        contract['reason'] = 'peak_shaving_owner'
        return result
    request_ts = number(request.get('ts'))
    window = tariff_window(config, now)
    eligible = bool(safe and config.get('heat_tariff_shift_mode') == 'active'
                    and request.get('mode') == 'active' and (request.get('would_start') is True or request.get('active') is True)
                    and request_ts is not None and 0 <= now - request_ts <= 30
                    and window['valid'] and (request.get('window') or {}).get('revision') == window['revision'])
    if not eligible:
        return result
    # Die Wärmepumpe steckt bereits in der Netz-/Akkubilanz. Ihre gemessene
    # Leistung wird genau einmal aus dem autonomen Entladebedarf herausgenommen.
    # Das Verbraucherbudget und seine Lastbilanz werden NICHT erneut gekürzt.
    measured = number(live.get('WP_Power', live.get('Heatpump_Power')))
    evidence_ts = number(live.get('Heatpump_Evidence_TS'))
    if (live.get('Heatpump_Power_Known') is not True or measured is None or measured < 0
            or evidence_ts is None or not 0 <= now - evidence_ts <= 10):
        contract['reason'] = 'measurement_missing_balance_unchanged'
        return result
    contract.update(hold_w=measured, reason='measured_heatpump_hold')
    if not request.get('active'):
        return result
    # Nur den bestehenden AUTO-Rahmen begrenzen. Ein anderer Speicher-Owner
    # wird nicht verdrängt und kein Akku-Netzladen angefordert.
    if result.get('mode') != 0:
        contract.update(safety_free=False, reason='storage_owner_conflict')
        return result
    cap = number(result.get('max_discharge_w'))
    if cap is None:
        contract.update(safety_free=False, reason='discharge_limit_missing')
        return result
    peak_priority = 0.
    if peak_enabled:
        required = number(peak.get('required_discharge_w'))
        peak_cap = number(peak.get('peak_max_discharge_w'))
        peak_ts = number(peak.get('updated_ts'))
        sample_ts = number(peak.get('sample_ts'))
        interval_start = number(peak.get('interval_start_ts'))
        interval_end = number(peak.get('interval_end_ts'))
        base_import = number(peak.get('base_import_w'))
        storage_kwh = number(peak.get('storage_capacity_kwh'))
        remaining_s = number(peak.get('remaining_s'))
        if (required is None or required < 0 or peak_cap is None or peak_cap < 0
                or peak_ts is None or not 0 <= now - peak_ts <= 10
                or sample_ts is None or sample_ts != round(ts, 3) or peak_ts != sample_ts
                or interval_start != int(sample_ts // 900) * 900 or interval_end != interval_start + 900
                or base_import is None or base_import < 0 or storage_kwh is None or storage_kwh <= 0
                or remaining_s is None or not 1 <= remaining_s <= 900
                # Restzeit wie im Erzeuger: Stichprobe, mindestens eine Sekunde.
                or abs(remaining_s - max(1., interval_end - sample_ts)) > 0.001):
            # Kein aktueller Kappungsvertrag: Startfreigabe erhalten, aber
            # keine zusätzliche Entladebegrenzung aus dem Tarifauftrag.
            contract['reason'] = 'peak_shaving_context_missing'
            return result
        peak_priority = min(required, peak_cap, max(0., cap))
        # Dieselben Energie- und Bezugsgrenzen wie evaluate_peak_shaving:
        # verfügbare Wh oberhalb der physischen Reserve über die Restzeit.
        reserve_energy_cap_w = max(0, math.floor(
            storage_kwh * 1000. * max(0., soc - reserve) / 100. * 3600. / remaining_s))
        peak_priority = min(peak_priority, reserve_energy_cap_w, math.ceil(base_import))
    contract['peak_priority_w'] = peak_priority
    residual = max(0., grid - battery - measured)
    auto = copy.deepcopy(result.get('auto_limit') or {})
    if auto.get('enabled') and not auto.get('release'):
        existing = number(auto.get('max_discharge_w'))
        if existing is None:
            contract.update(safety_free=False, reason='existing_limit_invalid')
            return result
        cap = min(cap, existing)
        # Auch eine ungeschützte Fremdgrenze bleibt verbindlich.
        peak_priority = min(peak_priority, max(0., existing))
    contract['peak_priority_w'] = peak_priority
    charge = number(auto.get('max_charge_w')) if auto.get('enabled') and not auto.get('release') else number(result.get('max_charge_w'))
    if charge is None:
        contract.update(safety_free=False, reason='charge_limit_missing')
        return result
    # Wie beim Ladegrenzenregler: Totband und 30-s-Bremse. Harte Vetos,
    # fehlende Messung und Vertragsende wurden bereits vor dieser Bremse geprüft.
    previous = (previous_state or {}).get('heat_tariff_shift') or {}
    previous_cap = number(previous.get('discharge_cap_w'))
    changed_ts = number(previous.get('changed_ts'))
    previous_ts = number(previous.get('ts'))
    bound = min(cap, residual)
    reusable = bool(previous.get('active') is True and previous_cap is not None
                    and changed_ts is not None and previous_ts is not None
                    and 0 <= now - previous_ts <= 30
                    and previous.get('intent_revision') == request.get('intent_revision'))
    if reusable and measured == 0:
        # Ohne WP-Abnahme nicht absenken; zusätzliche Hauslast weiter versorgen.
        bound = min(cap, max(previous_cap, residual))
    elif reusable and (abs(bound - previous_cap) <= deadband_w
                     or 0 <= now - changed_ts < min_interval_s):
        # Eine inzwischen strengere Schutzgrenze wird niemals angehoben.
        bound = min(cap, previous_cap)
    # Kappung wirkt im selben Zyklus, auch innerhalb von Totband und Bremse.
    # Nur die Tarifgrenze wird angehoben; Kappungs- und Hardwaregrenze führen.
    if peak_priority > bound:
        bound = peak_priority
        contract['reason'] = 'peak_shaving_priority'
    if not reusable or bound != previous_cap:
        changed_ts = now
    auto.update(enabled=True, release=False, max_charge_w=int(charge), max_discharge_w=int(bound),
                discharge_start_w=0, heartbeat_s=2., reason='Tariffenster: gemessene WP-Leistung aus dem Netz')
    result['auto_limit'] = auto
    contract.update(active=True, discharge_cap_w=auto['max_discharge_w'], changed_ts=changed_ts)
    return result
