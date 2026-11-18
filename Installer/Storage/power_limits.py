"""Bestehender Leistungsleser mit optionaler Dienstuhr."""
import time
from . import leistungsleser as _rein
from .leistungsleser import safe_float, safe_int, _configured_power_limit_w, _rscp_power_limit_w

def _fresh_rscp_charge_limits_w(cfg, live, *, now_s=None):
    return _rein._fresh_rscp_charge_limits_w(cfg, live, now_s=time.time() if now_s is None else now_s)

def _legacy_configured_charge_limit_w(cfg, live=None, *, now_s=None):
    return _rein._legacy_configured_charge_limit_w(cfg, live, now_s=time.time() if now_s is None else now_s)

def _legacy_configured_discharge_limit_w(cfg, live=None, fallback_charge_w=None):
    return _rein._legacy_configured_discharge_limit_w(cfg, live, fallback_charge_w, now_s=time.time())

def effective_power_limit_cfg(cfg, live=None, *, now_s=None, fallback_charge_w=None):
    return _rein.effective_power_limit_cfg(cfg, live, now_s=time.time() if now_s is None else now_s,
                                           fallback_charge_w=fallback_charge_w)
