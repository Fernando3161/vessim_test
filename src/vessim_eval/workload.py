"""Minimal job-based workload model retrofitted onto Vessim (E03).

Vessim models demand as a power signal; it has no jobs, queues or deadlines. This module adds
them through Vessim's own extension points:

- :class:`ClusterActor` — a custom ``Actor`` whose ``sample()`` admits arriving jobs, asks a
  start rule when to run each one and returns the IT power of the server pool, averaged
  exactly over the coming interval.
- Start rules (``on_arrival``, ``cheapest_price``, ``lowest_peak``, ``hold``) decide the start
  time of delay-tolerant jobs. Forecast-based rules run inside the actor (no delay).
- :class:`ImportHoldController` — a ``Controller`` that holds delay-tolerant jobs while the
  observed grid import is above a threshold. Controllers run after dispatch, so the hold
  decision only takes effect at the next sample (one-step delay).
- :class:`CoolingProxy` — cooling power as a linear function of IT power (stand-in for a
  thermal model, see E04). It reads the cluster actor's power of the same tick, which relies
  on Vessim sampling actors in list order.

Units: kW and seconds in public helpers, W inside Vessim.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

import numpy as np
import pandas as pd
import vessim as vs

StartRule = Literal["on_arrival", "cheapest_price", "lowest_peak", "hold"]

# --------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------


def extract_jobs(jobs_json: Path, out_csv: Path) -> pd.DataFrame:
    """Compact CSV of an edge study job file (one row per job, only the fields used here)."""
    raw = json.loads(Path(jobs_json).read_text())
    df = pd.DataFrame({
        "job_id": [j["id"] for j in raw],
        "arrival_s": [j["arrival_time"] for j in raw],
        "cpu": [round(j["cpu"], 3) for j in raw],
        "runtime_s": [j["runtime"] for j in raw],
        "delay_tolerant": [bool(j["temporal_flexibility"]["delay_tolerant"]) for j in raw],
        "max_delay_s": [j["temporal_flexibility"]["max_delay"] for j in raw],
    })
    df.to_csv(out_csv, index=False)
    return df


@dataclass
class ServerPool:
    """Linear CPU power model of the edge study (``simulators/hardware.py``).

    P = base load + idle + (peak − idle) · utilisation, with peak = W per CPU · CPUs.
    """

    capacity_cpu: float = 614_000
    p_peak_w_per_cpu: float = 7.3
    idle_fraction: float = 0.217
    base_load_kw: float = 900.0

    @property
    def peak_kw(self) -> float:
        return self.p_peak_w_per_cpu * self.capacity_cpu / 1e3

    def power_kw(self, used_cpu):
        """IT power (kW) for a CPU allocation; works on scalars and arrays."""
        util = np.asarray(used_cpu) / self.capacity_cpu
        return self.base_load_kw + self.peak_kw * (self.idle_fraction
                                                   + (1 - self.idle_fraction) * util)

    @property
    def kw_per_cpu(self) -> float:
        return self.peak_kw * (1 - self.idle_fraction) / self.capacity_cpu


# --------------------------------------------------------------------------------------
# Cluster actor
# --------------------------------------------------------------------------------------


class ClusterActor(vs.Consumer):
    """Job-based IT load as a Vessim actor.

    At each sample ``t`` the actor (1) admits jobs arriving in ``[t, t + step)``,
    (2) fixes their start time with the start rule, (3) releases held jobs, and (4) returns the
    IT power averaged over ``[t, t + step)``. Delay-intolerant jobs always start on arrival.
    Allocated CPU is tracked on a slot grid (``step_s``) with exact fractional overlaps, so
    the energy is exact for any step size.

    Args:
        name: Actor name.
        jobs: Job table (``arrival_s``, ``cpu``, ``runtime_s``, ``delay_tolerant``,
            ``max_delay_s``).
        pool: Server pool power model.
        step_s: Simulation step (must equal the microgrid step).
        horizon_s: Simulation horizon. Forecast-based rules only choose starts before it.
        rule: Start rule for delay-tolerant jobs.
        price: Day-ahead price on the slot grid (€/MWh), needed by ``cheapest_price``.
        other_net_kw: Forecast of the rest of the site's net load (kW, slot grid) and
            ``cooling_slope``: used by ``lowest_peak`` to predict grid import.
    """

    def __init__(self, name: str, jobs: pd.DataFrame, pool: ServerPool, step_s: int,
                 horizon_s: int, rule: StartRule = "on_arrival",
                 price: Optional[np.ndarray] = None,
                 other_net_kw: Optional[np.ndarray] = None, cooling_slope: float = 0.0):
        super().__init__(name, signal=0.0)
        self.jobs = jobs.reset_index(drop=True)
        self.pool, self.step_s, self.horizon_s, self.rule = pool, step_s, horizon_s, rule
        self.price, self.other_net_kw, self.cooling_slope = price, other_net_kw, cooling_slope
        self.n_slots = horizon_s // step_s
        extra = int(np.ceil((jobs.runtime_s.max() + jobs.max_delay_s.max()) / step_s)) + 2
        self.slot_cpu = np.zeros(self.n_slots + extra)   # mean allocated CPU per slot
        self.start_s = np.full(len(jobs), np.nan)
        self.queue: list[int] = []                         # held delay-tolerant jobs
        self.hold = False                                  # set by ImportHoldController
        self.overcommitted_jobs = 0
        self.last_power_kw = pool.power_kw(0.0)
        self._arr = self.jobs.arrival_s.to_numpy()
        self._order = np.argsort(self._arr, kind="stable")
        self._next = 0
        self.queue_log: dict[int, int] = {}

    # -- slot-grid bookkeeping -----------------------------------------------------------
    def _overlap(self, start: float, runtime: float) -> tuple[int, np.ndarray]:
        """First slot index and fraction of each slot covered by [start, start + runtime)."""
        k0 = int(start // self.step_s)
        k1 = int(np.ceil((start + runtime) / self.step_s))
        edges = np.arange(k0, k1 + 1) * self.step_s
        lo = np.maximum(edges[:-1], start)
        hi = np.minimum(edges[1:], start + runtime)
        return k0, (hi - lo) / self.step_s

    def _commit(self, j: int, start: float) -> None:
        cpu, rt = self.jobs.cpu[j], self.jobs.runtime_s[j]
        k0, frac = self._overlap(start, rt)
        if (self.slot_cpu[k0:k0 + len(frac)] + cpu * frac).max() > self.pool.capacity_cpu + 1e-6:
            self.overcommitted_jobs += 1
        self.slot_cpu[k0:k0 + len(frac)] += cpu * frac
        self.start_s[j] = start

    # -- start rules ---------------------------------------------------------------------
    def _candidates(self, a: float, latest: float) -> np.ndarray:
        """Arrival time plus every slot boundary up to the latest start (within horizon)."""
        latest = min(latest, self.horizon_s - self.step_s)
        k_first = int(np.floor(a / self.step_s)) + 1
        k_last = int(np.floor(latest / self.step_s))
        return np.concatenate([[a], np.arange(k_first, k_last + 1) * self.step_s])

    def _choose(self, j: int) -> Optional[float]:
        a, d = self._arr[j], self.jobs.max_delay_s[j]
        if self.rule == "on_arrival" or d <= 0:
            return a
        if self.rule == "hold":
            return None
        cpu, rt = self.jobs.cpu[j], self.jobs.runtime_s[j]
        best, best_score = a, np.inf
        for s in self._candidates(a, a + d):
            k0, frac = self._overlap(s, rt)
            load = self.slot_cpu[k0:k0 + len(frac)] + cpu * frac
            if load.max() > self.pool.capacity_cpu:
                continue
            if self.rule == "cheapest_price":
                p = self.price[k0:k0 + len(frac)]
                p = np.where(np.isnan(p), np.nanmean(self.price), p)   # beyond horizon
                score = float((p * frac).sum())
            else:  # lowest_peak: predicted site import over the job's runtime
                other = self.other_net_kw[k0:k0 + len(frac)]
                other = np.where(np.isnan(other), 0.0, other)
                score = float(((1 + self.cooling_slope) * self.pool.power_kw(load) + other).max())
            if score < best_score - 1e-9:
                best, best_score = s, score
        return best

    # -- Vessim actor interface ----------------------------------------------------------
    def sample(self, elapsed):
        t = float(elapsed.total_seconds() if hasattr(elapsed, "total_seconds") else elapsed)
        t_end = t + self.step_s
        while self._next < len(self._order) and self._arr[self._order[self._next]] < t_end:
            j = self._order[self._next]
            self._next += 1
            start = self._choose(j)
            if start is None:
                self.queue.append(j)
            else:
                self._commit(j, start)
        still = []
        for j in self.queue:                         # hold rule: release or force at deadline
            a, deadline = self._arr[j], self._arr[j] + self.jobs.max_delay_s[j]
            if not self.hold:
                self._commit(j, max(a, t))
            elif deadline < t_end:
                self._commit(j, max(deadline, t))
            else:
                still.append(j)
        self.queue = still
        k = int(t // self.step_s)
        waiting = int(((self._arr < t_end) & (np.isnan(self.start_s) | (self.start_s >= t_end))).sum())
        self.queue_log[int(t)] = waiting
        self.last_power_kw = float(self.pool.power_kw(self.slot_cpu[k]))
        power = -self.last_power_kw * 1e3
        return power, {"power": power, "waiting_jobs": waiting,
                       "utilization": float(self.slot_cpu[k] / self.pool.capacity_cpu)}

    # -- results -------------------------------------------------------------------------
    def job_table(self) -> pd.DataFrame:
        """Jobs with their start time and start delay (s); unstarted jobs have NaN start."""
        out = self.jobs.copy()
        out["start_s"] = self.start_s
        out["delay_s"] = out.start_s - out.arrival_s
        out["deadline_missed"] = out.delay_s > out.max_delay_s + 1e-6
        return out

    def energy_beyond_horizon_kwh(self) -> float:
        """Dynamic IT energy of started jobs that falls after the horizon."""
        return float(self.slot_cpu[self.n_slots:].sum() * self.step_s / 3600 * self.pool.kw_per_cpu)


class CoolingProxy(vs.Consumer):
    """Cooling power = slope · IT power + intercept (kW), from the IT actor's current sample.

    Must be listed *after* the cluster actor: Vessim samples actors in list order.
    """

    def __init__(self, name: str, it_actor: ClusterActor, slope: float, intercept_kw: float):
        super().__init__(name, signal=0.0)
        self.it_actor, self.slope, self.intercept_kw = it_actor, slope, intercept_kw

    def sample(self, elapsed):
        power = -max(0.0, self.slope * self.it_actor.last_power_kw + self.intercept_kw) * 1e3
        return power, {"power": power}


class ImportHoldController(vs.Controller):
    """Hold delay-tolerant jobs while observed grid import exceeds a threshold.

    Observes the tick *after* dispatch and sets ``cluster.hold`` for the next sample, so every
    decision is applied one step late. Held jobs are forced to start at their deadline.
    """

    def __init__(self, cluster: ClusterActor, microgrid: str, threshold_kw: float):
        self.cluster, self.microgrid, self.threshold_kw = cluster, microgrid, threshold_kw

    def step(self, now, microgrid_states) -> None:
        import_kw = -microgrid_states[self.microgrid]["p_grid"] / 1e3  # Vessim: + = export
        self.cluster.hold = import_kw > self.threshold_kw


# --------------------------------------------------------------------------------------
# Running and evaluating
# --------------------------------------------------------------------------------------


def slot_series(s: pd.Series, start: pd.Timestamp, step_s: int, n: int) -> np.ndarray:
    """Forward-fill a time series onto the simulation slot grid (NaN beyond the series)."""
    idx = start + pd.to_timedelta(np.arange(n) * step_s, unit="s")
    out = s.reindex(idx, method="ffill").to_numpy(dtype=float, copy=True)
    out[idx >= s.index[-1] + (s.index[1] - s.index[0])] = np.nan
    return out


def run_workload(
    jobs: pd.DataFrame,
    pool: ServerPool,
    building_kw: pd.Series,
    pv_kw: pd.Series,
    price: pd.Series,
    cooling: tuple[float, float],
    rule: StartRule = "on_arrival",
    step_s: int = 60,
    horizon_s: int = 86_400,
    hold_threshold_kw: Optional[float] = None,
) -> dict:
    """Run the site (cluster + cooling proxy + building + PV, grid only) with one start rule.

    Returns a dict with the standardized time series (kW, grid + = import), the job table,
    the cluster actor and the wall time.
    """
    import time

    from vessim_eval.scenarios import _trace_w

    start = building_kw.index[0]
    n = (horizon_s // step_s
         + int(np.ceil((jobs.runtime_s.max() + jobs.max_delay_s.max()) / step_s)) + 2)
    slope, intercept = cooling
    price_slots = slot_series(price, start, step_s, n)
    other = (slot_series(building_kw, start, step_s, n) - slot_series(pv_kw, start, step_s, n)
             + intercept)
    cluster = ClusterActor("it", jobs, pool, step_s, horizon_s, rule=rule, price=price_slots,
                           other_net_kw=other, cooling_slope=slope)
    actors = [cluster, CoolingProxy("cooling", cluster, slope, intercept),
              vs.Consumer("building", signal=_trace_w(building_kw)),
              vs.Producer("pv", signal=_trace_w(pv_kw))]
    env = vs.Environment(sim_start=start, step_size=step_s)
    env.add_microgrid(name="site", actors=actors, dispatchables=[vs.GridConnection()])
    if rule == "hold":
        env.add_controller(ImportHoldController(cluster, "site", hold_threshold_kw))
    rec = env.add_controller(vs.Recorder())
    t0 = time.perf_counter()
    env.run(until=horizon_s)
    wall = time.perf_counter() - t0
    env.close()
    df = rec.to_df().xs("site", level="microgrid")
    ts = pd.DataFrame(index=pd.DatetimeIndex(df.index, name="time"))
    ts["it_kw"] = -df["actor_states.it.power"].to_numpy() / 1e3
    ts["cooling_kw"] = -df["actor_states.cooling.power"].to_numpy() / 1e3
    ts["grid_kw"] = -df["p_grid"].to_numpy() / 1e3
    ts["waiting_jobs"] = df["actor_states.it.waiting_jobs"].to_numpy()
    ts["utilization"] = df["actor_states.it.utilization"].to_numpy()
    ts["price"] = price.reindex(ts.index, method="ffill").to_numpy()
    return {"ts": ts, "jobs": cluster.job_table(), "cluster": cluster, "wall_s": wall,
            "step_s": step_s, "cooling_slope": slope}


def run_static(it_kw: pd.Series, cooling_kw: pd.Series, building_kw: pd.Series,
               pv_kw: pd.Series, price: pd.Series) -> dict:
    """Vessim-native baseline: IT and cooling as exogenous power traces (no jobs)."""
    import time

    from vessim_eval.scenarios import _trace_w

    step_s = int((it_kw.index[1] - it_kw.index[0]).total_seconds())
    actors = [vs.Consumer(n, signal=_trace_w(s)) for n, s in
              (("it", it_kw), ("cooling", cooling_kw), ("building", building_kw))]
    actors.append(vs.Producer("pv", signal=_trace_w(pv_kw)))
    env = vs.Environment(sim_start=it_kw.index[0], step_size=step_s)
    env.add_microgrid(name="site", actors=actors, dispatchables=[vs.GridConnection()])
    rec = env.add_controller(vs.Recorder())
    t0 = time.perf_counter()
    env.run(until=len(it_kw) * step_s)
    wall = time.perf_counter() - t0
    env.close()
    df = rec.to_df().xs("site", level="microgrid")
    ts = pd.DataFrame(index=pd.DatetimeIndex(df.index, name="time"))
    ts["it_kw"] = -df["actor_states.it.power"].to_numpy() / 1e3
    ts["cooling_kw"] = -df["actor_states.cooling.power"].to_numpy() / 1e3
    ts["grid_kw"] = -df["p_grid"].to_numpy() / 1e3
    ts["price"] = price.reindex(ts.index, method="ffill").to_numpy()
    return {"ts": ts, "jobs": None, "cluster": None, "wall_s": wall, "step_s": step_s}


def workload_kpis(run: dict, reference_it_kw: Optional[pd.Series] = None) -> pd.Series:
    """Energy, cost, peak and job-level KPIs of one run.

    ``reference_it_kw`` (same index) gives the energy shifted: half the integrated absolute
    difference of IT power, i.e. the energy moved from one time to another.
    """
    ts, jobs, dt_h = run["ts"], run["jobs"], run["step_s"] / 3600
    if jobs is None:   # static power trace: energy KPIs only
        return pd.Series({
            "IT energy in the day (kWh)": ts.it_kw.sum() * dt_h,
            "Grid import energy (kWh)": ts.grid_kw.clip(lower=0).sum() * dt_h,
            "Energy cost (€)": (ts.grid_kw.clip(lower=0) * ts.price / 1000).sum() * dt_h,
            "Peak grid import (kW)": ts.grid_kw.max(),
            "Peak grid import, 15-min mean (kW)": ts.grid_kw.resample("15min").mean().max(),
            "Peak IT power (kW)": ts.it_kw.max(),
            "Wall time (s)": run["wall_s"],
        })
    flex = jobs[jobs.delay_tolerant]
    after_kwh = run["cluster"].energy_beyond_horizon_kwh()
    cost = (ts.grid_kw.clip(lower=0) * ts.price / 1000).sum() * dt_h
    started_late = flex.start_s.isna() | (flex.start_s >= run["cluster"].horizon_s)
    out = {
        "IT energy in the day (kWh)": ts.it_kw.sum() * dt_h,
        "IT energy after midnight (kWh)": after_kwh,
        "Grid import energy (kWh)": ts.grid_kw.clip(lower=0).sum() * dt_h,
        "Energy cost (€)": cost,
        # Shifting moves job energy past midnight, out of the costed day. Value it (with its
        # cooling) at the day's mean price so that variants stay comparable.
        "Cost incl. job energy after midnight (€)":
            cost + after_kwh * (1 + run["cooling_slope"]) * ts.price.mean() / 1000,
        "Peak grid import (kW)": ts.grid_kw.max(),
        "Peak grid import, 15-min mean (kW)": ts.grid_kw.resample("15min").mean().max(),
        "Peak IT power (kW)": ts.it_kw.max(),
        "Delay-tolerant jobs shifted": int((flex.delay_s > 1e-6).sum()),
        "Mean start delay, delay-tolerant (min)": flex.delay_s.mean() / 60,
        "Max start delay, delay-tolerant (min)": flex.delay_s.max() / 60,
        "Delay-tolerant jobs starting after midnight": int(started_late.sum()),
        "Deadline violations": int(jobs.deadline_missed.sum()),
        "Max waiting jobs": int(ts.waiting_jobs.max()),
        "Max CPU utilisation (%)": ts.utilization.max() * 100,
        "Jobs placed above CPU capacity": run["cluster"].overcommitted_jobs,
        "Wall time (s)": run["wall_s"],
    }
    if reference_it_kw is not None:
        out["Energy shifted (kWh)"] = 0.5 * (ts.it_kw - reference_it_kw).abs().sum() * dt_h
    return pd.Series(out)


__all__ = ["extract_jobs", "ServerPool", "ClusterActor", "CoolingProxy", "ImportHoldController",
           "slot_series", "run_workload", "run_static", "workload_kpis"]
