"""Laufzeitbelege und Tageskonto für die zentrale Tariffenster-Planung."""
import json
import os
from datetime import datetime
from pathlib import Path

from . import tariff_shift as policy
from .tariff_shift import TARIFF_TIMEZONE, number


class TariffShiftRuntime:
    def __init__(self, root='/var/www/html'):
        self.root = Path(root)
        self.state = None
        self.last_ts = None
        self.last_log_ts = None
        self.last_save_ts = None
        self.last_save_owner = None
        self.durable = False
        self.last_ww_running = None

    def _read(self, path):
        try:
            value = json.loads(path.read_text(), parse_constant=lambda value: None)
            return value if isinstance(value, dict) else {}
        except (ValueError, OSError):
            return {}

    def _save(self, path, value):
        temporary = path.with_suffix('.tmp')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open('w') as stream:
                json.dump(value, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            return True
        except (OSError, ValueError, TypeError):
            return False

    def cycle(self, ctx, checkpoint, *, now):
        try:
            return self._cycle(ctx, checkpoint, now=now)
        except Exception:  # Ungültige Zustände bleiben gesperrt, die Dienstschleife läuft weiter.
            decision = {'schema': 'heat_tariff_shift_v1', 'mode': 'off', 'ts': now,
                        'would_start': False, 'commands_allowed': False, 'active': False,
                        'hold_w': None, 'blockers': ['tariff_runtime_data_invalid']}
            self._save(self.root / 'ramdisk/heat_tariff_shift.json', decision)
            return decision

    def _cycle(self, ctx, checkpoint, *, now):
        cfg = policy.migrate(ctx.get('current_config') or {})
        mode = cfg.get('heat_tariff_shift_mode')
        if mode not in ('shadow', 'active'):
            return {'schema': 'heat_tariff_shift_v1', 'mode': 'off', 'ts': now,
                    'would_start': False, 'commands_allowed': False, 'blockers': ['mode_off'],
                    'hold_w': None}
        day = datetime.fromtimestamp(now, TARIFF_TIMEZONE).date().isoformat()
        state_path = self.root / 'data/heat_tariff_shift_state.json'
        if self.state is None:
            self.state = self._read(state_path)
            if not self.state and state_path.exists():
                self.state = {'day': day, 'used_s': None, 'reason': 'daily_state_unreadable'}
        if self.state and (not isinstance(self.state.get('day'), str)
                           or len(self.state['day']) != 10):
            raise ValueError('Ungültiger Tag im Tarifkonto')
        if not self.state or self.state.get('day', '') < day:
            self.state = {'day': day, 'used_s': 0., 'used_kwh': 0., 'ww_durations_s':
                          self.state.get('ww_durations_s', []) if self.state else []}
        state = self.state
        for key in ('used_s', 'used_kwh'):
            value = number(state.get(key))
            state[key] = value if value is not None and value >= 0 else None
        if state.get('day') != day:
            state['used_s'] = None
        channels = checkpoint.get('channels') or {}
        owned = [(k, v) for k, v in channels.items() if v.get('owner') == 'tariff' and v.get('possible_effect')]
        elapsed = None if self.last_ts is None else now - self.last_ts
        self.last_ts = now
        if elapsed is None:
            state.pop('ww_observed_start', None)
        if owned and elapsed is None:
            # Unbeobachtete Laufzeit nach Neustart ist kein kostenloses Budget.
            saved = number(state.get('last_ts'))
            elapsed = None if saved is None else max(0., now - saved)
            if elapsed is None:
                state['used_s'] = None
        if owned and elapsed is not None and elapsed >= 0:
            if number(state.get('used_s')) is not None:
                state['used_s'] += elapsed
            limit = number(cfg.get('wp_pv_max_power_w'))
            measured = number((ctx.get('heatpump_pv_contract') or {}).get('observation', {}).get('power_w'))
            if ctx.get('wp_compressor_observation_valid') is True and ctx.get('wp_compressor_running_now') is True and measured is not None:
                counted = measured
            else:
                counted = limit
            if counted is None:
                state['used_kwh'] = None
            elif number(state.get('used_kwh')) is not None:
                state['used_kwh'] += counted * elapsed / 3600000
        if owned:
            state['active_target'], active = owned[0]
            state['request_id'] = active['request_id']
        else:
            state.pop('active_target', None)
            state.pop('request_id', None)
        ww_running = (ctx.get('luxtronik_ww_runtime_contract') or {}).get('ww_running')
        if ww_running is True and self.last_ww_running is False and state.get('ww_observed_start') is None:
            state['ww_observed_start'] = now
            state['ww_observed_target'] = number((ctx.get('wp_data') or {}).get('Warmwasser_Soll'))
        elif ww_running is False and state.get('ww_observed_start') is not None:
            span = now - state.pop('ww_observed_start')
            actual = number((ctx.get('wp_data') or {}).get('Warmwasser_Ist'))
            target = number(state.pop('ww_observed_target', None))
            if elapsed is not None and 0 <= elapsed <= 60 and 60 <= span <= 14400 and actual is not None and target is not None and actual >= target:
                state['ww_durations_s'] = (state.get('ww_durations_s', []) + [span])[-8:]
        if ww_running not in (True, False) or (elapsed is not None and not 0 <= elapsed <= 60):
            state.pop('ww_observed_start', None)
        self.last_ww_running = ww_running
        from .plan_forecast import read_plan_forecast
        live = read_plan_forecast(self.root / 'ramdisk/storage_plan.json')
        forecast = policy.forecast_to_next_pv(live, now)
        missing = number(forecast.get('missing_kwh'))
        if missing is not None:
            used = number(state.get('used_kwh'))
            forecast['missing_kwh'] = None if used is None else max(0., missing - used)
        status = ctx.get('wp_status') or {}
        data = ctx.get('wp_data') or {}
        storage = self._read(self.root / 'ramdisk/storage_manager_state.json')
        grant = storage.get('heat_tariff_shift') or {}
        grant_ts = number(grant.get('ts'))
        fresh_grant = grant_ts is not None and 0 <= now - grant_ts <= 30
        evidence = {
            'forecast': forecast,
            'data_fresh': ctx.get('e3dc_valid') is True and status.get('valid') is True and status.get('source_fresh') is True,
            'reserve_free': ctx.get('heatpump_user_boost_emergency_veto') == '',
            'emergency_free': ctx.get('heatpump_user_boost_emergency_veto') == '',
            'storage_free': fresh_grant and grant.get('safety_free') is True,
            'device_free': not ctx.get('heatpump_signal_typed_protection_stop') and data.get('Betriebsart') != 3,
            'source_free': number(ctx.get('wq_aus')) is not None and number(ctx.get('WQ_MIN_TEMP')) is not None and ctx['wq_aus'] >= ctx['WQ_MIN_TEMP'],
            'holiday_free': ctx.get('wp_is_vacation') is False,
            'user_free': not bool(set(ctx.get('heatpump_positive_output_block_reasons') or []) & {
                'manual_user_off', 'automatic_mode_user_off', 'manual_source_temperature_stop',
                'manual_low_soc_stop', 'pre_control_independent_safety_stop', 'independent_safety_stop',
                'source_recovery_pause', 'legacy_pv_pause'}),
            'restart_free': not bool(ctx.get('price_heatpump_takt_start_blocked')),
            'power_limit_w': number(cfg.get('wp_pv_max_power_w')),
            'ww': {'actual_c': number(data.get('Warmwasser_Ist')), 'target_c': number(ctx.get('CONF_WWW'))},
            'hz': {'actual_c': number(data.get('Ruecklauf_Ist')), 'target_c': number(ctx.get('CONF_HZ'))},
            'higher_owner': bool((ctx.get('heatpump_pv_output') or {}).get('start') or (ctx.get('heatpump_pv_output') or {}).get('keep') or ctx.get('predump_heatpump_active') or ctx.get('manual_ww_requested_before_io')),
        }
        # Kein Ersatzwert: denselben Mitteltemperatursensor wie die Heizgrenze nutzen.
        outside, heating_limit = number(data.get('Aussentemp_Mittel')), number(ctx.get('HEIZGRENZE_TEMP'))
        evidence['outside_mean_c'] = outside
        evidence['summer'] = (outside > heating_limit) if outside is not None and heating_limit is not None else None
        if evidence['summer'] is True:
            evidence['ww']['target_c'] = number(ctx.get('CONF_WWS'))
        from .policy import decide_tariff_shift
        decision = decide_tariff_shift(cfg, evidence, state, now=now)
        if decision.get('commands_allowed') and (not fresh_grant or grant.get('intent_revision') != decision['intent_revision']):
            decision['commands_allowed'] = False
            decision['blockers'].append('storage_grant_pending')
        # Eine reine Speicherbelegung beendet keinen laufenden Wärmeauftrag
        # vor seiner Mindestlaufzeit; echte Schutzvetos bleiben sofort wirksam.
        decision['storage_safety_veto'] = bool(fresh_grant and grant.get('safety_veto') is True)
        decision['used_s'] = state.get('used_s')
        decision['used_kwh'] = state.get('used_kwh')
        decision['power_limit_w'] = evidence['power_limit_w']
        decision['active'] = bool(owned)
        state['last_ts'] = now
        owner_key = [(name, value.get('request_id')) for name, value in owned]
        if self.last_save_ts is None or now - self.last_save_ts >= 60 or owner_key != self.last_save_owner:
            self.durable = self._save(state_path, state)
            if self.durable:
                self.last_save_ts, self.last_save_owner = now, owner_key
        if not self.durable:
            decision['commands_allowed'] = False
            decision['blockers'].append('daily_state_not_durable')
        # Diagnose enthält ausschließlich den typisierten, personenfreien Vertrag.
        self._save(self.root / 'ramdisk/heat_tariff_shift.json', decision)
        log_path = self.root / ('logs/heat_tariff_shift_' + day + '.jsonl')
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if self.last_log_ts is None or now - self.last_log_ts >= 60:
                record = {key: decision.get(key) for key in ('ts', 'mode', 'would_start', 'commands_allowed', 'blockers', 'target', 'target_c', 'start_ts', 'remaining_daily_s', 'used_s', 'used_kwh')}
                record['forecast'] = {key: forecast.get(key) for key in ('valid', 'reason', 'need_kwh', 'pv_cover_kwh', 'missing_kwh', 'pv_method', 'load_method', 'horizon_end_ts')}
                with log_path.open('a') as stream:
                    stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
                records = []
                for line in log_path.read_text().splitlines()[-1440:]:
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        continue
                self._save(self.root / 'ramdisk/heat_tariff_shift_day.json', {'day': day, 'records': records})
                self.last_log_ts = now
        except (OSError, ValueError):
            decision['diagnostic_export_error'] = True
        return decision
