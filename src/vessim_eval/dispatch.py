"""Dispatch policies, controllers and battery builders for E02.

Everything here uses Vessim's extension points only (``DispatchPolicy``, ``Controller``,
``Storage``). Units inside Vessim are W / Wh with "positive = absorbing" for dispatchables;
public helpers take and return kW / kWh / €/MWh with the report conventions (grid
positive = import, battery positive = discharge).
"""

from __future__ import annotations

import time
from typing import Literal, Optional, Sequence

import numpy as np
import pandas as pd
import vessim as vs
from scipy.optimize import linprog
from scipy.sparse import lil_matrix
from vessim.dispatchable import GridConnection, Storage

from vessim_eval.scenarios import BatteryParams, SiteInputs, _trace_w, make_battery, standardize

BatteryKind = Literal["simple", "losses", "clc"]

# --------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------


def price_from_reference(ref: pd.DataFrame) -> pd.Series:
    """Day-ahead electricity price (€/MWh) used by the edge study run.

    The edge study fetches hourly DE-LU day-ahead prices (energy-charts.info, €/kWh) and
    interpolates them to its step. The cached reference does not store the price, but its LP
    columns do: ``lp_energy_cost_eur = lp_grid_kw * price * dt``. This recovers the price
    exactly while grid import is non-zero (always the case at this site).
    """
    dt_h = (ref.index[1] - ref.index[0]).total_seconds() / 3600
    price = ref["lp_energy_cost_eur"] / (ref["lp_grid_kw"] * dt_h) * 1000
    return price.rename("price_eur_per_mwh")


def build_battery(kind: BatteryKind, b: BatteryParams):
    """Battery dispatchable: Vessim ``SimpleBattery``, loss-aware ``EfficiencyBattery`` or
    Vessim's electrochemical ``ClcBattery`` scaled to the same capacity.

    ``ClcBattery`` is parameterised per cell (default LG M50 21700, 19.14 Wh). It is scaled by
    the number of cells and its C-rates are set so that the pack limits equal the edge study's
    charge/discharge power. It has no upper SOC limit; the internal efficiencies stay at the
    cell defaults (charge 97.8 %, discharge 1/1.014 = 98.6 %).
    """
    if kind in ("simple", "losses"):
        return make_battery(kind, b)
    cell_wh = 19.14
    n_cells = int(round(b.capacity_kwh * 1e3 / cell_wh))
    return vs.ClcBattery(
        "battery", number_of_cells=n_cells, initial_soc=b.soc_init, min_soc=b.soc_min,
        alpha_d=-b.max_discharge_kw / b.capacity_kwh, alpha_c=b.max_charge_kw / b.capacity_kwh,
    )


# --------------------------------------------------------------------------------------
# Custom dispatch policies (each is the complete control logic; grid balances the rest)
# --------------------------------------------------------------------------------------


def _balance_with_grid(p_delta: float, duration: float, dispatchables, storage_power: dict):
    """Set storage setpoints (clipped to feasible range); grid connections absorb the rest."""
    remaining = p_delta
    grids = []
    for d in dispatchables:
        if isinstance(d, GridConnection):
            grids.append(d)
            continue
        lo, hi = d.feasible_range(duration)
        p = min(max(storage_power.get(d.name, 0.0), lo), hi)
        d.set_power(p, duration)
        remaining -= p
    for i, g in enumerate(grids):
        lo, hi = g.feasible_range(duration)
        p = remaining if i == len(grids) - 1 else min(max(remaining, lo), hi)
        g.set_power(p, duration)
        remaining -= p


class PriceThresholdPolicy(vs.DispatchPolicy):
    """Charge at full power when the price is low, discharge when it is high.

    Reads the price from the grid signal ``price`` (€/MWh). Storages are charged when
    price <= ``low`` and discharged when price >= ``high``; otherwise they stay idle.
    Feasible-range clipping enforces SOC and power limits.
    """

    def __init__(self, low: float, high: float, signal: str = "price") -> None:
        self.low, self.high, self.signal = low, high, signal
        self._mode = "idle"

    def apply(self, p_delta, duration, dispatchables, grid_signals=None) -> None:
        price = (grid_signals or {})[self.signal]
        self._mode = "charge" if price <= self.low else "discharge" if price >= self.high else "idle"
        target = {"charge": np.inf, "discharge": -np.inf, "idle": 0.0}[self._mode]
        _balance_with_grid(p_delta, duration, dispatchables,
                           {d.name: target for d in dispatchables if isinstance(d, Storage)})

    def state(self) -> dict:
        return {"mode": self._mode}


class PeakShavingPolicy(vs.DispatchPolicy):
    """Keep grid import at or below a threshold using only the current imbalance.

    ``p_delta`` < 0 is the deficit the grid would have to import. Above the threshold the
    storage discharges the excess; below it, the storage recharges with the spare headroom
    (never pushing import above the threshold). Needs no forecast, but the threshold
    itself must be chosen in advance.
    """

    def __init__(self, threshold_kw: float) -> None:
        self.threshold_w = threshold_kw * 1e3

    def apply(self, p_delta, duration, dispatchables, grid_signals=None) -> None:
        import_w = -p_delta
        # positive = charge (Vessim sign): headroom below threshold, negative = shave excess
        target = self.threshold_w - import_w
        _balance_with_grid(p_delta, duration, dispatchables,
                           {d.name: target for d in dispatchables if isinstance(d, Storage)})

    def state(self) -> dict:
        return {"threshold_w": self.threshold_w}


class TimeOfUseController(vs.Controller):
    """Switch ``DefaultDispatchPolicy.charge_power`` by time of day (native building blocks).

    ``windows`` is a list of (start hour, end hour, power kW, + = charge). Outside all windows
    the battery should be idle. ``charge_power = 0`` does *not* mean idle in Vessim — it
    switches back to greedy priority dispatch — so idle is emulated with a negligible
    ``IDLE_W`` setpoint. Controllers run after dispatch; the controller therefore decides at
    ``now`` for the interval starting at ``now + step_s``. The very first interval uses the
    policy's initial ``charge_power``.
    """

    IDLE_W = 1e-6

    def __init__(self, policy: vs.DefaultDispatchPolicy,
                 windows: Sequence[tuple[float, float, float]], step_s: int) -> None:
        self.policy, self.windows, self.step_s = policy, list(windows), step_s

    def power_w(self, when: pd.Timestamp) -> float:
        hour = when.hour + when.minute / 60 + when.second / 3600
        power_kw = next((p for a, b, p in self.windows if a <= hour < b), None)
        return self.IDLE_W if power_kw is None else power_kw * 1e3

    def step(self, now, microgrid_states) -> None:
        self.policy.charge_power = self.power_w(now + pd.Timedelta(seconds=self.step_s))


# --------------------------------------------------------------------------------------
# Look-ahead: perfect-foresight LP, solved outside Vessim and replayed as a schedule
# --------------------------------------------------------------------------------------


def optimal_schedule(net_load_kw: pd.Series, price: pd.Series, b: BatteryParams,
                     objective: Literal["cost", "peak"] = "cost",
                     throughput_penalty: float = 1e-4) -> pd.Series:
    """Day-ahead battery schedule (kW, + = discharge) with perfect foresight.

    Same formulation as the edge study's ``battery_optimizer.compute_offline_optimal``:
    SOC dynamics with charge/discharge efficiencies, SOC window, power limits, final SOC
    pinned to the initial SOC; objective = energy cost or peak grid import.
    ``throughput_penalty`` (per kWh charged or discharged) breaks ties between equally
    optimal schedules — the pure peak objective is highly degenerate — in favour of the
    schedule with the least battery cycling.
    """
    n = len(net_load_kw)
    dt_h = (net_load_kw.index[1] - net_load_kw.index[0]).total_seconds() / 3600
    cap = b.capacity_kwh
    peak = objective == "peak"
    nv = 3 * n + int(peak)
    c = np.zeros(nv)
    if peak:
        c[3 * n] = 1.0
    else:
        p = price.to_numpy() / 1000  # €/kWh
        c[:n], c[n:2 * n] = -p * dt_h, p * dt_h
    c[:2 * n] += throughput_penalty * dt_h
    bounds = ([(0, b.max_discharge_kw)] * n + [(0, b.max_charge_kw)] * n
              + [(b.soc_min * cap, b.soc_max * cap)] * (n - 1)
              + [(b.soc_init * cap, b.soc_init * cap)] + ([(0, None)] if peak else []))
    a_eq = lil_matrix((n, nv))
    b_eq = np.zeros(n)
    for t in range(n):
        a_eq[t, t], a_eq[t, n + t], a_eq[t, 2 * n + t] = dt_h / b.eff_dis, -b.eff_ch * dt_h, 1
        if t == 0:
            b_eq[0] = b.soc_init * cap
        else:
            a_eq[t, 2 * n + t - 1] = -1
    a_ub = b_ub = None
    if peak:
        a_ub = lil_matrix((n, nv))
        for t in range(n):
            a_ub[t, t], a_ub[t, n + t], a_ub[t, 3 * n] = -1, 1, -1
        a_ub, b_ub = a_ub.tocsr(), -net_load_kw.to_numpy()
    res = linprog(c, A_ub=a_ub, b_ub=b_ub, A_eq=a_eq.tocsr(), b_eq=b_eq, bounds=bounds,
                  method="highs")
    if not res.success:
        raise RuntimeError(res.message)
    return pd.Series(res.x[:n] - res.x[n:2 * n], index=net_load_kw.index,
                     name="battery_setpoint_kw")


# --------------------------------------------------------------------------------------
# Running and evaluating
# --------------------------------------------------------------------------------------


def run_policy(
    inputs: SiteInputs,
    battery: BatteryParams,
    policy: vs.DispatchPolicy,
    battery_kind: BatteryKind = "losses",
    price: Optional[pd.Series] = None,
    schedule_kw: Optional[pd.Series] = None,
    controllers: Sequence = (),
    step_s: Optional[int] = None,
) -> tuple[pd.DataFrame, float]:
    """Run the reference site with a given policy; return (standardized frame, wall time s).

    ``price`` (€/MWh) is passed as grid signal ``price``; ``schedule_kw`` (+ = discharge) as
    grid signal ``setpoint_battery_w`` for :class:`~vessim_eval.vessim_models.SetpointPolicy`.
    ``controllers`` are callables ``f(policy, battery) -> vs.Controller``.
    """
    step_s = step_s or inputs.step_s
    actors = [
        vs.Consumer("it", signal=_trace_w(inputs.it_kw)),
        vs.Consumer("cooling", signal=_trace_w(inputs.cooling_kw)),
        vs.Consumer("building", signal=_trace_w(inputs.building_kw)),
        vs.Producer("pv", signal=_trace_w(inputs.pv_kw)),
    ]
    bat = build_battery(battery_kind, battery)
    signals = {}
    if price is not None:
        signals["price"] = _trace_w(price, scale=1.0)
    if schedule_kw is not None:
        signals["setpoint_battery_w"] = _trace_w(schedule_kw, scale=-1000.0)
    env = vs.Environment(sim_start=inputs.start, step_size=step_s)
    env.add_microgrid(name="site", actors=actors, dispatchables=[bat, vs.GridConnection()],
                      policy=policy, grid_signals=signals or None)
    for make in controllers:
        env.add_controller(make(policy, bat))
    rec = env.add_controller(vs.Recorder())
    t0 = time.perf_counter()
    env.run(until=inputs.horizon_s)
    wall = time.perf_counter() - t0
    env.close()
    out = standardize(rec.to_df())
    out.loc[out.index[-1], "soc_end"] = bat.soc()
    out.attrs.update(step_s=step_s, final_stored_kwh=bat.state()["charge_level"] / 1e3,
                     final_soc=bat.soc())
    return out, wall


def policy_kpis(df: pd.DataFrame, price: pd.Series, b: BatteryParams) -> pd.Series:
    """Cost, peak, energy and battery KPIs of a standardized run.

    Price is forward-filled onto the run's time index. Equivalent full cycles = discharged
    energy / nameplate capacity. ``SOC window respected`` checks soc_min <= SOC <= soc_max
    (with 1e-9 tolerance) at every step and at the end.

    Strategies end the day with different stored energy, so the raw energy cost rewards
    emptying the battery. ``Cost incl. change in stored energy`` adds the energy drawn from
    (or left in) the battery, valued at the day's mean price.
    """
    dt_h = df.attrs["step_s"] / 3600
    p = price.reindex(df.index, method="ffill")
    imp = df["grid_kw"].clip(lower=0)
    charge = (-df["battery_kw"]).clip(lower=0).sum() * dt_h
    discharge = df["battery_kw"].clip(lower=0).sum() * dt_h
    socs = pd.concat([df["soc"], pd.Series([df.attrs["final_soc"]])])
    d_stored = df.attrs["final_stored_kwh"] - b.capacity_kwh * b.soc_init
    cost = (imp * p / 1000).sum() * dt_h
    return pd.Series({
        "Energy cost (€)": cost,
        "Cost incl. change in stored energy (€)": cost - d_stored * price.mean() / 1000,
        "Peak grid import (kW)": imp.max(),
        "Grid import energy (kWh)": imp.sum() * dt_h,
        "Battery charge energy (kWh)": charge,
        "Battery discharge energy (kWh)": discharge,
        "Battery losses (kWh)": charge - discharge - d_stored,
        "Equivalent full cycles": discharge / b.capacity_kwh,
        "Minimum state of charge (%)": socs.min() * 100,
        "Maximum state of charge (%)": socs.max() * 100,
        "Final state of charge (%)": df.attrs["final_soc"] * 100,
        "SOC window respected": bool(socs.min() >= b.soc_min - 1e-9
                                     and socs.max() <= b.soc_max + 1e-9),
    })


def logic_lines(*objs) -> int:
    """Lines of code of classes/functions, excluding blank lines, comments and docstrings."""
    import ast
    import inspect
    import textwrap

    total = 0
    for obj in objs:
        tree = ast.parse(textwrap.dedent(inspect.getsource(obj)))
        doc_lines: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and ast.get_docstring(node):
                d = node.body[0]
                doc_lines.update(range(d.lineno, d.end_lineno + 1))
        src = textwrap.dedent(inspect.getsource(obj)).splitlines()
        total += sum(1 for i, line in enumerate(src, start=1)
                     if line.strip() and not line.strip().startswith("#") and i not in doc_lines)
    return total


__all__ = ["logic_lines", "price_from_reference", "build_battery", "PriceThresholdPolicy",
           "PeakShavingPolicy", "TimeOfUseController", "optimal_schedule", "run_policy",
           "policy_kpis"]
