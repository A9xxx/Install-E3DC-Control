"""S10 slave controller: pure policy, positive battery power means charging."""
from dataclasses import dataclass, asdict
import math

@dataclass(frozen=True)
class Settings:
    max_charge_w: int = 3000
    max_discharge_w: int = 3000
    reserve_pct: float = 10
    resume_discharge_pct: float = 12
    charge_stop_pct: float = 100
    charge_resume_pct: float = 98
    buffer_w: float = 200
    deadband_w: float = 75
    step_w: int = 50
    interval_s: float = 5
    neutral_s: float = 20
    max_age_s: float = 15
    night_pv_max_w: float = 100
    import_guard_w: float = 80
    master_discharge_guard_w: float = 100
    night_enabled: bool = False
    master_capacity_wh: float = 0
    slave_capacity_wh: float = 0
    master_reserve_pct: float = 10
    discharge_start_w: int = 100

    def __post_init__(self):
        for k,v in asdict(self).items():
            if not isinstance(v,(int,float)) or not math.isfinite(v):
                raise ValueError('invalid setting '+k)
        if not (0 <= self.reserve_pct < self.resume_discharge_pct < self.charge_resume_pct < self.charge_stop_pct <= 100):
            raise ValueError('invalid SoC boundaries')
        if min(self.max_charge_w,self.max_discharge_w,self.step_w,self.interval_s,self.max_age_s) <= 0 or self.neutral_s < 0 or self.buffer_w <= self.deadband_w or self.deadband_w < 0:
            raise ValueError('invalid limits')
        if not 0 <= self.master_reserve_pct < 100 or self.night_pv_max_w < 0 or min(self.import_guard_w,self.master_discharge_guard_w) < 0:
            raise ValueError('invalid reserve/guards')
        if self.night_enabled and min(self.master_capacity_wh,self.slave_capacity_wh) <= 0:
            raise ValueError('night mode needs both usable capacities')
        if type(self.discharge_start_w) is not int or not 0 <= self.discharge_start_w <= 2**32-1:
            raise ValueError('invalid discharge start threshold')

@dataclass(frozen=True)
class Reading:
    ts: float
    budget_w: float
    pv_w: float
    grid_w: float
    master_battery_w: float
    master_soc: float
    slave_battery_w: float
    slave_soc: float
    valid: bool = True

@dataclass(frozen=True)
class Decision:
    mode: str
    watts: int
    reason: str

class Controller:
    def __init__(self, settings=Settings()):
        self.c=settings; self.power=0; self.last_step=float('-inf')
        self.neutral_until=0; self.last_sample=float('-inf')
        self.full=False; self.empty=False

    def stop(self, now, reason):
        self.power=0; self.last_step=now
        self.neutral_until=max(self.neutral_until, now+self.c.neutral_s)
        return Decision('idle',0,reason)

    def decision(self, reason):
        return Decision('charge' if self.power>0 else 'discharge' if self.power<0 else 'idle',abs(self.power),reason)

    def tick(self, r, now):
        c=self.c
        values=[v for k,v in asdict(r).items() if k!='valid']
        if not math.isfinite(now) or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in values) or not r.valid or not (-2 <= now-r.ts <= c.max_age_s) or not (0 <= r.master_soc <=100 and 0 <= r.slave_soc <=100):
            return self.stop(now,'stale_or_invalid')
        if r.slave_soc>=c.charge_stop_pct:self.full=True
        elif r.slave_soc<=c.charge_resume_pct:self.full=False
        if r.slave_soc<=c.reserve_pct:self.empty=True
        elif r.slave_soc>=c.resume_discharge_pct:self.empty=False
        if self.power>0 and (self.full or r.grid_w>c.import_guard_w or r.master_battery_w < -c.master_discharge_guard_w):
            return self.stop(now,'charge_guard')
        if self.power<0 and (self.empty or r.pv_w>c.night_pv_max_w or r.grid_w < -c.import_guard_w or r.master_battery_w>c.master_discharge_guard_w):
            return self.stop(now,'discharge_guard')
        if r.ts <= self.last_sample or now-self.last_step<c.interval_s:
            return self.decision('await_fresh_sample')
        self.last_sample=r.ts; self.last_step=now
        if now < self.neutral_until:return self.decision('neutral_dwell')
        available=min(max(0,r.budget_w),max(0,-r.grid_w))
        charge_ok=not self.full and r.pv_w>c.night_pv_max_w and r.grid_w<=c.import_guard_w and r.master_battery_w>=-c.master_discharge_guard_w
        discharge_ok=c.night_enabled and not self.empty and r.pv_w<=c.night_pv_max_w and r.grid_w>=-c.import_guard_w and r.master_battery_w<=c.master_discharge_guard_w
        target=0
        if charge_ok and (available>c.buffer_w+c.deadband_w or (self.power>0 and available>=c.buffer_w-c.deadband_w)):
            if self.power<0:return self.stop(now,'direction_change')
            target=self.power
            if available>c.buffer_w+c.deadband_w:target+=c.step_w
        elif self.power>0:
            target=max(0,self.power-c.step_w)
        elif discharge_ok:
            # Shared residual demand, with both batteries removed from grid balance.
            # DC/AC losses are not known: this is a conservative approximate split.
            demand=max(0,r.grid_w-r.master_battery_w-r.slave_battery_w)
            master_energy=c.master_capacity_wh*max(0,r.master_soc-c.master_reserve_pct)/100
            slave_energy=c.slave_capacity_wh*max(0,r.slave_soc-c.reserve_pct)/100
            total=master_energy+slave_energy
            target=-min(c.max_discharge_w,demand*slave_energy/total if total else 0)
        else:
            target=0
        target=int(round(max(-c.max_discharge_w,min(c.max_charge_w,target))))
        if target*self.power<0:return self.stop(now,'direction_change')
        if target<0:
            minimum=c.discharge_start_w
            start=minimum+c.deadband_w if minimum else 0
            if -target<minimum or (self.power>=0 and -target<start):
                return self.stop(now,'below_discharge_start')
            target=max(self.power-c.step_w,min(self.power+c.step_w,target))
            # Cross the start threshold once, then use the normal small ramp.
            if minimum and -target<minimum:target=-minimum
        if target==0 and self.power!=0:return self.stop(now,'neutral')
        self.power=target
        return self.decision('bounded_budget_control')

