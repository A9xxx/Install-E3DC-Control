"""Unveränderliche Wallboxzyklus-Eingaben für den modularen Prüfpfad."""

from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Any, Dict, Mapping, Tuple


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class ChargerCycleContext:
    wb_id: int
    public_mode: int
    control_mode: int
    allowed_w: float = 0.0
    cap_amp: float = 0.0
    detected_phases: int = 1
    max_amp: int = 0
    connected: bool = False
    current_amp: float = 0.0
    current_set_amp: float = 0.0
    hw_charging: bool = False
    hw_power_w: float = 0.0
    grid_power_w: float = 0.0
    mode_label: str = ""
    storage_state: str = ""
    driver_class_name: str = ""
    openwb_like: bool = False
    openwb_pro: bool = False
    e3dc_native_toggle: bool = False
    observe_only: bool = False
    priority_forced_stop: bool = False
    budget_timeout: bool = False
    current_decision: Mapping[str, Any] = field(default_factory=dict)
    start_stop_decision: Mapping[str, Any] = field(default_factory=dict)
    phase_recommendation: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "current_decision", _freeze(self.current_decision))
        object.__setattr__(self, "start_stop_decision", _freeze(self.start_stop_decision))
        object.__setattr__(self, "phase_recommendation", _freeze(self.phase_recommendation))

    def as_dict(self) -> Dict[str, Any]:
        return {item.name: _thaw(getattr(self, item.name)) for item in fields(self)}


@dataclass(frozen=True)
class CycleContext:
    now_ts: float
    chargers: Tuple[ChargerCycleContext, ...] = ()
    config_revision: str = ""
    budget_source: str = ""
    schedule_active: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "now_ts": self.now_ts,
            "chargers": [charger.as_dict() for charger in self.chargers],
            "config_revision": self.config_revision,
            "budget_source": self.budget_source,
            "schedule_active": self.schedule_active,
        }
