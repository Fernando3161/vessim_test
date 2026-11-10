"""Build and run small Vessim scenarios from kW input traces.

All public functions take and return kW / kWh with the conventions used in the
reports: grid power positive = import, battery power positive = discharge.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal, Optional

import pandas as pd
import vessim as vs

from vessim_eval.vessim_models import (
    EdgeSurplusChargingController,
    EfficiencyBattery,
    SetpointPolicy,
)

BatteryModel = Literal["simple", "losses"]
Control = Literal["greedy", "edge_rule", "schedule"]


@dataclass
class BatteryParams:
    """Battery parameters in edge study units (kWh, kW, fractions)."""

    capacity_kwh: float = 500.0
    soc_init: float = 0.5
    soc_min: float = 0.15
    soc_max: float = 0.9
    max_charge_kw: float = 250.0
    max_discharge_kw: float = 250.0
    eff_ch: float = 0.95
    eff_dis: float = 0.95

    @classmethod
    def from_edge_config(cls, cfg: dict) -> "BatteryParams":
        return cls(
            capacity_kwh=cfg["capacity"], soc_init=cfg["soc_init"], soc_min=cfg["soc_min"],
            soc_max=cfg["soc_max"], max_charge_kw=cfg["max_charge_power"],
            max_discharge_kw=cfg["max_discharge_power"], eff_ch=cfg["eff_ch"],
            eff_dis=cfg["eff_dis"],
        )


@dataclass
class SiteInputs:
    """Exogenous site time series in kW on a regular DatetimeIndex."""

    it_kw: pd.Series
    building_kw: pd.Series
    cooling_kw: pd.Series
    pv_kw: pd.Series
    hardware_base_load_kw: float = 0.0
    cooling_overhead: float = 0.285  # edge controller assumption (0.95 * 0.3)

    @classmethod
    def from_reference(cls, ref: pd.DataFrame, hardware_base_load_kw: float,
                       mean: bool = False) -> "SiteInputs":
        """Inputs from an extracted edge reference frame.

        ``mean=False`` uses values sampled at each step start (edge accounting);
        ``mean=True`` uses time-weighted step means (energy-conserving).
        """
        sfx = "_mean_kw" if mean else "_kw"
        return cls(ref["it" + sfx], ref["building" + sfx], ref["cooling" + sfx],
                   ref["pv" + sfx], hardware_base_load_kw=hardware_base_load_kw)

    @property
    def step_s(self) -> int:
        return int((self.it_kw.index[1] - self.it_kw.index[0]).total_seconds())

    @property
    def start(self) -> pd.Timestamp:
        return self.it_kw.index[0]

    @property
    def horizon_s(self) -> int:
        return len(self.it_kw) * self.step_s


def _trace_w(s: pd.Series, scale: float = 1000.0) -> vs.Trace:
    """kW series with DatetimeIndex -> offset-indexed Vessim Trace in W (forward-filled).

    The last value is repeated at the end of its step so that simulations with a finer
    step size than the trace can query the whole last interval (Trace raises on overflow).
    """
    offsets = list((s.index - s.index[0]).total_seconds().astype(int))
    values = list(s.values * scale)
    if len(offsets) > 1:
        offsets.append(2 * offsets[-1] - offsets[-2])
        values.append(values[-1])
    return vs.Trace(pd.Series(values, index=offsets))


def make_battery(model: BatteryModel, b: BatteryParams):
    """Vessim SimpleBattery or the loss-aware EfficiencyBattery from edge parameters."""
    if model == "simple":
        # SimpleBattery: no efficiencies, no upper SOC limit, one symmetric C-rate.
        return vs.SimpleBattery("battery", capacity=b.capacity_kwh * 1e3,
                                initial_soc=b.soc_init, min_soc=b.soc_min,
                                c_rate=b.max_charge_kw / b.capacity_kwh)
    return EfficiencyBattery("battery", capacity_wh=b.capacity_kwh * 1e3,
                             initial_soc=b.soc_init, min_soc=b.soc_min, max_soc=b.soc_max,
                             max_charge_w=b.max_charge_kw * 1e3,
                             max_discharge_w=b.max_discharge_kw * 1e3,
                             eff_ch=b.eff_ch, eff_dis=b.eff_dis)


def run_site(
    inputs: SiteInputs,
    battery: BatteryParams,
    battery_model: BatteryModel = "simple",
    control: Control = "greedy",
    schedule_discharge_kw: Optional[pd.Series] = None,
    step_s: Optional[int] = None,
    recorder_output=None,
) -> tuple[pd.DataFrame, float]:
    """Run one Vessim configuration of the site; return (time series, wall time in s).

    ``step_s`` overrides the simulation step (default: the input trace step; finer
    steps forward-fill the inputs). ``recorder_output`` writes Vessim's experiment
    record to that folder. Vessim's built-in recorder metrics for the site are
    attached as ``result.attrs["vessim_metrics"]``.

    control
        ``"greedy"``: Vessim's DefaultDispatchPolicy (battery absorbs every surplus
        and covers every deficit, grid balances the rest).
        ``"edge_rule"``: the edge study's online battery rule, implemented as a
        Vessim Controller writing setpoints into a SetpointPolicy (one-step delay).
        ``"schedule"``: replay a given battery schedule (kW, positive = discharge),
        passed to the policy as a grid signal so it applies in the same interval.
    """
    step_s = step_s or inputs.step_s
    actors = [
        vs.Consumer("it", signal=_trace_w(inputs.it_kw)),
        vs.Consumer("cooling", signal=_trace_w(inputs.cooling_kw)),
        vs.Consumer("building", signal=_trace_w(inputs.building_kw)),
        vs.Producer("pv", signal=_trace_w(inputs.pv_kw)),
    ]
    bat = make_battery(battery_model, battery)
    grid_signals = None
    if control == "greedy":
        policy = vs.DefaultDispatchPolicy()
    else:
        policy = SetpointPolicy()
        if control == "schedule":
            if schedule_discharge_kw is None:
                raise ValueError("control='schedule' needs schedule_discharge_kw.")
            # Vessim sign: positive = absorb (charge) -> negate the discharge schedule.
            grid_signals = {"setpoint_battery_w": _trace_w(schedule_discharge_kw, scale=-1000.0)}

    env = vs.Environment(sim_start=inputs.start, step_size=step_s)
    env.add_microgrid(name="site", actors=actors, dispatchables=[bat, vs.GridConnection()],
                      policy=policy, grid_signals=grid_signals)
    if control == "edge_rule":
        lossy = battery_model == "losses"
        env.add_controller(EdgeSurplusChargingController(
            policy, "site", bat, pv_actor="pv", building_actor="building",
            hardware_base_load_w=inputs.hardware_base_load_kw * 1e3,
            cooling_overhead=inputs.cooling_overhead,
            max_charge_w=battery.max_charge_kw * 1e3,
            max_soc=battery.soc_max if lossy else 1.0,
            eff_ch=battery.eff_ch if lossy else 1.0,
            capacity_wh=battery.capacity_kwh * 1e3, step_s=step_s,
        ))
    rec = env.add_controller(vs.Recorder(recorder_output))
    t0 = time.perf_counter()
    env.run(until=inputs.horizon_s)
    wall = time.perf_counter() - t0
    env.close()
    out = standardize(rec.to_df())
    out.loc[out.index[-1], "soc_end"] = bat.soc()
    out.attrs["vessim_metrics"] = dict(rec.metrics["microgrids"]["site"])
    out.attrs["final_stored_kwh"] = bat.state()["charge_level"] / 1e3
    out.attrs["step_s"] = step_s
    return out, wall


def standardize(df: pd.DataFrame, microgrid: str = "site") -> pd.DataFrame:
    """Vessim recorder frame (W, Vessim signs) -> kW with report sign conventions.

    Recorded states are observed at each tick after dispatch, so row ``t`` holds the
    setpoints for interval [t, t + step) and the SOC at the *start* of that interval
    (``soc``). ``soc_end`` is the SOC at the end of the interval (the edge study's
    measurement convention); its last value is filled by :func:`run_site`.
    """
    df = df.xs(microgrid, level="microgrid")
    out = pd.DataFrame(index=pd.DatetimeIndex(df.index, name="time"))
    out["grid_kw"] = -df["p_grid"].to_numpy() / 1e3                                  # + = import
    out["battery_kw"] = -df["dispatch_states.battery.current_power"].to_numpy() / 1e3  # + = discharge
    out["soc"] = df["dispatch_states.battery.soc"].to_numpy()
    out["soc_end"] = out["soc"].shift(-1)
    out["stored_kwh"] = df["dispatch_states.battery.charge_level"].to_numpy() / 1e3
    out["pv_kw"] = df["actor_states.pv.power"].to_numpy() / 1e3
    demand_cols = [c for c in df.columns if c.startswith("actor_states.")
                   and c.endswith(".power") and c != "actor_states.pv.power"]
    out["demand_kw"] = -df[demand_cols].sum(axis=1).to_numpy() / 1e3
    out["balance_residual_kw"] = (out["demand_kw"] - out["pv_kw"] - out["battery_kw"]
                                  - out["grid_kw"])
    return out


def energy_closure(df: pd.DataFrame, initial_stored_kwh: float) -> pd.Series:
    """Daily energy balance of a standardized run (kWh), including battery losses.

    Supply = PV + grid import + battery discharge; use = demand + grid export +
    battery charge. ``Balance error`` must be ~0. Battery losses are the charge/discharge
    throughput not reflected in the change of stored energy.
    """
    dt_h = df.attrs.get("step_s", 900) / 3600
    charge = (-df["battery_kw"]).clip(lower=0).sum() * dt_h
    discharge = df["battery_kw"].clip(lower=0).sum() * dt_h
    imp = df["grid_kw"].clip(lower=0).sum() * dt_h
    exp = (-df["grid_kw"]).clip(lower=0).sum() * dt_h
    pv = df["pv_kw"].sum() * dt_h
    demand = df["demand_kw"].sum() * dt_h
    d_stored = df.attrs["final_stored_kwh"] - initial_stored_kwh
    return pd.Series({
        "Photovoltaic generation": pv, "Grid import": imp, "Battery discharge": discharge,
        "Site demand": demand, "Grid export": exp, "Battery charge": charge,
        "Change in stored energy": d_stored,
        "Battery losses": charge - discharge - d_stored,
        "Balance error": (pv + imp + discharge) - (demand + exp + charge),
    })


def recorder_check(df: pd.DataFrame) -> pd.DataFrame:
    """Vessim's built-in recorder metrics vs integration of the recorded time series."""
    m = df.attrs["vessim_metrics"]
    dt_h = df.attrs.get("step_s", 900) / 3600
    own = {
        "grid_import": df["grid_kw"].clip(lower=0).sum() * dt_h * 1e3,
        "grid_export": (-df["grid_kw"]).clip(lower=0).sum() * dt_h * 1e3,
        "energy_produced": df["pv_kw"].sum() * dt_h * 1e3,
        "energy_consumed": df["demand_kw"].sum() * dt_h * 1e3,
        "peak_grid_import": df["grid_kw"].max() * 1e3,
    }
    rows = {k: {"Vessim recorder": m[k], "Own integration": v, "Difference": m[k] - v}
            for k, v in own.items() if k in m}
    return pd.DataFrame(rows).T


def agreement(reference: pd.Series, candidate: pd.Series, step_s: int) -> pd.Series:
    """Agreement metrics between two kW (or fraction) series on the same index."""
    diff = candidate.reindex(reference.index) - reference
    dt_h = step_s / 3600
    return pd.Series({
        "Maximum absolute difference": diff.abs().max(),
        "Mean absolute difference": diff.abs().mean(),
        "Daily energy difference (kWh)": diff.sum() * dt_h,
    })


def energy_summary(df: pd.DataFrame, step_s: int) -> pd.Series:
    """Energy KPIs (kWh / kW) from a standardized time series."""
    dt_h = step_s / 3600
    return pd.Series({
        "Grid import energy (kWh)": df["grid_kw"].clip(lower=0).sum() * dt_h,
        "Grid export energy (kWh)": (-df["grid_kw"]).clip(lower=0).sum() * dt_h,
        "Peak grid import (kW)": df["grid_kw"].max(),
        "Battery charge energy (kWh)": (-df["battery_kw"]).clip(lower=0).sum() * dt_h,
        "Battery discharge energy (kWh)": df["battery_kw"].clip(lower=0).sum() * dt_h,
        "Minimum state of charge (%)": df["soc"].min() * 100,
        "Maximum state of charge (%)": df["soc"].max() * 100,
    })
