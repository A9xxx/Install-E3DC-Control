"""Set the native discharge threshold once; verify without refresh writes."""
from dataclasses import replace
import time

class PowerSettingsError(RuntimeError):
    pass

def integer(value, name):
    if type(value) is not int or not 0 <= value <= 2**32-1:
        raise PowerSettingsError('invalid_power_setting_' + name)
    return value

def read_settings(device):
    raw = device.get_power_settings()
    if not isinstance(raw, dict) or type(raw.get('powerLimitsUsed')) is not bool:
        raise PowerSettingsError('power_settings_unavailable')
    return dict(powerLimitsUsed=raw['powerLimitsUsed'], **{
        key: integer(raw.get(key), key)
        for key in ('maxChargePower', 'maxDischargePower', 'dischargeStartPower')})

class PowerLimits:
    """One setup attempt per process; changed settings require operator review."""
    def __init__(self, device, settings, execute=False, communication_errors=()):
        self.device, self.settings, self.execute = device, settings, execute
        self.communication_errors = communication_errors
        self.expected, self.next_check, self.started = None, 0, False
        self.receipt = {'status': 'observation_only'}

    def start(self, now):
        if self.started:
            raise PowerSettingsError('power_settings_setup_already_attempted')
        self.started = True
        if not self.execute:
            return self.settings
        before = read_settings(self.device)
        for key, attribute in (('maxChargePower', 'maxBatChargePower'),
                               ('maxDischargePower', 'maxBatDischargePower')):
            hardware = integer(getattr(self.device, attribute, None), attribute)
            if hardware <= 0 or before[key] > hardware:
                raise PowerSettingsError('power_limits_exceed_hardware')
        threshold = max(self.settings.discharge_start_w, before['dischargeStartPower'])
        expected = dict(before, powerLimitsUsed=True, dischargeStartPower=threshold)
        self.receipt = {'status': 'setup_unconfirmed', 'before': before, 'requested': expected}
        if before != expected:
            # Explicit limits avoid the library fallback to the hardware maxima.
            try:
                result = self.device.set_power_limits(enable=True, discharge_start=threshold,
                    max_charge=before['maxChargePower'], max_discharge=before['maxDischargePower'])
            except self.communication_errors as exc:
                # Unbestätigten Schreibversuch ausschließlich durch Lesen klären.
                self.receipt['write_error'] = type(exc).__name__
            else:
                if type(result) is not int or result not in (0, 1):
                    raise PowerSettingsError('power_settings_rejected')
        for delay in (0, 0.5, 1):
            if delay:
                time.sleep(delay)
            actual = read_settings(self.device)
            self.receipt['actual'] = actual
            if actual == expected:
                break
        if actual != expected:
            self.receipt['status'] = 'power_settings_readback_mismatch'
            raise PowerSettingsError('power_settings_readback_mismatch')
        self.expected, self.next_check = expected, now + 30
        self.receipt = dict(self.receipt, status='settings_readback_confirmed', actual=expected)
        return replace(self.settings, discharge_start_w=threshold)

    def check(self, now, force=False):
        if not self.execute:
            return
        if self.expected is None:
            raise PowerSettingsError('power_settings_not_confirmed')
        if force or now >= self.next_check:
            actual = read_settings(self.device)
            self.receipt = dict(self.receipt, actual=actual)
            if actual != self.expected:
                self.receipt['status'] = 'settings_changed_or_unconfirmed'
                raise PowerSettingsError('power_settings_changed_stop_without_rewrite')
            self.next_check = now + 30

    def bound(self, mode, watts):
        if self.expected is None:
            raise PowerSettingsError('power_settings_not_confirmed')
        if mode == 'idle':
            return mode, 0
        key = 'maxChargePower' if mode == 'charge' else 'maxDischargePower'
        watts = min(watts, self.expected[key])
        if watts <= 0 or (mode == 'discharge' and watts < self.expected['dischargeStartPower']):
            return 'idle', 0
        return mode, watts
