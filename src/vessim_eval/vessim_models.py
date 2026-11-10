"""Custom Vessim components that mirror edge data center study models.

Vessim sign convention (dispatchables): positive power = absorbing (charging, export),
negative = supplying (discharging, import). Units are W and Wh.
"""

from __future__ import annotations

import vessim as vs
from vessim.dispatchable import GridConnection, Storage


class EfficiencyBattery(Storage):
    """Battery with charge/discharge efficiency, SOC window and power limits.

    Mirrors ``simulators/battery_storage.py`` of the edge study:
    stored energy increases by ``P * eff_ch * dt`` when charging and decreases by
    ``|P| / eff_dis * dt`` when discharging. ``current_power`` is measured at the
    grid side of the battery.

    Args:
        name: Component name.
        capacity_wh: Usable nameplate capacity in Wh.
        initial_soc, min_soc, max_soc: State of charge as fraction of capacity.
        max_charge_w, max_discharge_w: Power limits in W (positive numbers).
        eff_ch, eff_dis: Charge and discharge efficiencies (0, 1].
    """

    def __init__(
        self,
        name: str,
        capacity_wh: float,
        initial_soc: float = 0.5,
        min_soc: float = 0.0,
        max_soc: float = 1.0,
        max_charge_w: float = float("inf"),
        max_discharge_w: float = float("inf"),
        eff_ch: float = 1.0,
        eff_dis: float = 1.0,
    ) -> None:
        super().__init__(name)
        if not 0 <= min_soc <= initial_soc <= max_soc <= 1:
            raise ValueError("Require 0 <= min_soc <= initial_soc <= max_soc <= 1.")
        if not (0 < eff_ch <= 1 and 0 < eff_dis <= 1):
            raise ValueError("Efficiencies must be in (0, 1].")
        self.capacity_wh = capacity_wh
        self.energy_wh = capacity_wh * initial_soc
        self.min_soc, self.max_soc = min_soc, max_soc
        self.max_charge_w, self.max_discharge_w = max_charge_w, max_discharge_w
        self.eff_ch, self.eff_dis = eff_ch, eff_dis

    def feasible_range(self, duration: float) -> tuple[float, float]:
        dt_h = duration / 3600
        headroom_wh = max(0.0, self.max_soc * self.capacity_wh - self.energy_wh)
        available_wh = max(0.0, self.energy_wh - self.min_soc * self.capacity_wh)
        max_charge = min(self.max_charge_w, headroom_wh / (self.eff_ch * dt_h))
        max_discharge = min(self.max_discharge_w, available_wh * self.eff_dis / dt_h)
        return (-max_discharge, max_charge)

    def step(self, duration: float) -> None:
        dt_h = duration / 3600
        p = self.current_power
        if p > 0:
            self.energy_wh += p * self.eff_ch * dt_h
        elif p < 0:
            self.energy_wh += p / self.eff_dis * dt_h
        # Guard against floating-point drift past the SOC window.
        lo, hi = self.min_soc * self.capacity_wh, self.max_soc * self.capacity_wh
        self.energy_wh = min(max(self.energy_wh, lo), hi)

    def soc(self) -> float:
        return self.energy_wh / self.capacity_wh

    def config(self) -> dict:
        return {
            "capacity_wh": self.capacity_wh, "min_soc": self.min_soc, "max_soc": self.max_soc,
            "max_charge_w": self.max_charge_w, "max_discharge_w": self.max_discharge_w,
            "eff_ch": self.eff_ch, "eff_dis": self.eff_dis,
        }

    def state(self) -> dict:
        return {"soc": self.soc(), "charge_level": self.energy_wh,
                "current_power": self.current_power}


class SetpointPolicy(vs.DispatchPolicy):
    """Apply externally chosen storage setpoints; grid connections balance the rest.

    Setpoints come from a grid signal ``setpoint_<name>_w`` (schedule, same interval)
    or from ``setpoints`` written by a controller. Storages without a setpoint stay
    idle. Setpoints are clipped to the feasible range. A controller writes ``setpoints`` after
    observing a tick, so a setpoint takes effect in the *next* interval — the same
    one-step delay as the edge study's time-shifted mosaik connection.
    """

    def __init__(self) -> None:
        self.setpoints: dict[str, float] = {}

    def apply(self, p_delta, duration, dispatchables, grid_signals=None) -> None:
        remaining = p_delta
        grids = []
        signals = grid_signals or {}
        for d in dispatchables:
            if isinstance(d, GridConnection):
                grids.append(d)
                continue
            # A grid signal named "setpoint_<name>_w" (a time-varying schedule) takes
            # precedence over a controller-written setpoint. Policies receive no
            # timestamp, so grid signals are the only way to pass a schedule in.
            target = signals.get(f"setpoint_{d.name}_w", self.setpoints.get(d.name, 0.0))
            lo, hi = d.feasible_range(duration)
            p = min(max(target, lo), hi)
            d.set_power(p, duration)
            remaining -= p
        for i, g in enumerate(grids):
            lo, hi = g.feasible_range(duration)
            p = remaining if i == len(grids) - 1 else min(max(remaining, lo), hi)
            g.set_power(p, duration)
            remaining -= p

    def state(self) -> dict:
        return {f"setpoint_{k}": v for k, v in self.setpoints.items()}


class EdgeSurplusChargingController(vs.Controller):
    """Re-implementation of the edge study's battery rule (``controller.py``, step 4b).

    If renewable generation exceeds the *base load* (hardware base load incl. cooling
    overhead plus building demand), charge the battery with the surplus, limited by
    the charge power and remaining headroom. Never discharge.

    Args:
        policy: The SetpointPolicy of the microgrid.
        microgrid: Microgrid name.
        battery: Battery dispatchable (EfficiencyBattery or SimpleBattery).
        pv_actor: Name of the PV producer actor.
        building_actor: Name of the building consumer actor.
        hardware_base_load_w: Constant hardware base load in W.
        cooling_overhead: Cooling overhead fraction applied to hardware base load.
        max_charge_w: Charge power limit in W.
        max_soc: Upper SOC limit used for the headroom.
        eff_ch: Charge efficiency used for the headroom.
        step_s: Step size in seconds.
    """

    def __init__(
        self,
        policy: SetpointPolicy,
        microgrid: str,
        battery: Storage,
        pv_actor: str,
        building_actor: str,
        hardware_base_load_w: float,
        cooling_overhead: float,
        max_charge_w: float,
        max_soc: float,
        eff_ch: float,
        capacity_wh: float,
        step_s: int,
    ) -> None:
        self.policy, self.microgrid, self.battery = policy, microgrid, battery
        self.pv_actor, self.building_actor = pv_actor, building_actor
        self.base_w = hardware_base_load_w * (1 + cooling_overhead)
        self.max_charge_w, self.max_soc, self.eff_ch = max_charge_w, max_soc, eff_ch
        self.capacity_wh, self.dt_h = capacity_wh, step_s / 3600

    def step(self, now, microgrid_states) -> None:
        st = microgrid_states[self.microgrid]
        pv_w = st["actor_states"][self.pv_actor]["power"]
        building_w = -st["actor_states"][self.building_actor]["power"]
        surplus_w = pv_w - (self.base_w + building_w)
        soc_wh = self.battery.soc() * self.capacity_wh
        if surplus_w > 0:
            limit = min(self.max_charge_w,
                        max(0.0, self.max_soc * self.capacity_wh - soc_wh) / (self.eff_ch * self.dt_h))
            setpoint = min(surplus_w, limit)
        else:
            setpoint = 0.0
        self.policy.setpoints[self.battery.name] = setpoint


def default_policy() -> vs.DefaultDispatchPolicy:
    """Vessim's built-in greedy priority dispatch (for readability in notebooks)."""
    return vs.DefaultDispatchPolicy()


__all__ = ["EfficiencyBattery", "SetpointPolicy", "EdgeSurplusChargingController",
           "default_policy"]
