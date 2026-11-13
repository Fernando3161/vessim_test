"""Grid-serving flexibility in Vessim (E05): limits, demand response, bands, islanding.

Built on Vessim's extension points only:

- :class:`DeferrableLoad` — the delay-tolerant share of the IT load as a ``Dispatchable``
  (not an actor), so the dispatch policy can curtail it and catch up later. It keeps a
  deferral backlog (energy owed). Dispatchables receive no timestamp, so it keeps its own
  clock by summing the durations passed to ``step()``.
- :class:`BalanceSlack` — a fictitious resource that takes any imbalance the real resources
  cannot cover. Vessim requires an exact balance and raises otherwise; the slack turns an
  infeasible step into recorded unserved energy instead of an exception.
- :class:`GridServingPolicy` — keeps the grid inside its feasible range (import limit, ramps,
  disconnection) using the battery and the deferrable load, recovers backlog and SOC with the
  remaining headroom, and can be told to activate all flexibility through a grid signal.
- :class:`GridEventController` — applies an import limit and the connection state for the
  *next* interval from grid signals, as in Vessim's ``examples/demand_response.py``.
- :class:`FlexibilityBand` — records min / reference / max grid import per tick from the
  resources' ``feasible_range(duration)`` for several durations.

Units: kW / kWh / s in public helpers; W / Wh inside Vessim. Report sign convention:
grid positive = import, battery positive = discharge.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional, Sequence

import numpy as np
import pandas as pd
import vessim as vs
from vessim.dispatchable import Dispatchable, GridConnection, Storage

from vessim_eval.scenarios import BatteryParams, _trace_w, make_battery

H = 3600.0

# --------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------


@dataclass
class FlexSite:
    """Site traces (kW, regular DatetimeIndex) split into inflexible and deferrable demand."""

    inflexible_kw: pd.Series
    flexible_kw: pd.Series
    pv_kw: pd.Series
    price: pd.Series  # €/MWh

    @classmethod
    def from_reference(cls, ref: pd.DataFrame, price: pd.Series, flex_share: float) -> "FlexSite":
        """IT + cooling split by ``flex_share`` (delay-tolerant energy share); building is inflexible."""
        dc = ref["it_kw"] + ref["cooling_kw"]
        return cls(dc * (1 - flex_share) + ref["building_kw"], dc * flex_share, ref["pv_kw"],
                   price)

    @property
    def net_load_kw(self) -> pd.Series:
        return self.inflexible_kw + self.flexible_kw - self.pv_kw

    @property
    def start(self) -> pd.Timestamp:
        return self.pv_kw.index[0]

    @property
    def step_s(self) -> int:
        return int((self.pv_kw.index[1] - self.pv_kw.index[0]).total_seconds())

    @property
    def horizon_s(self) -> int:
        return len(self.pv_kw) * self.step_s


def window_series(index: pd.DatetimeIndex, windows: Sequence[tuple[str, str, float]],
                  default: float) -> pd.Series:
    """Piecewise-constant series: ``value`` on [start, end) for each (start 'HH:MM', end, value)."""
    s = pd.Series(default, index=index, dtype=float)
    tod = index - index.normalize()
    for a, b, v in windows:
        s[(tod >= pd.Timedelta(a + ":00")) & (tod < pd.Timedelta(b + ":00"))] = v
    return s


# --------------------------------------------------------------------------------------
# Dispatchables
# --------------------------------------------------------------------------------------


class DeferrableLoad(Dispatchable):
    """Flexible demand that can be curtailed into a backlog and caught up later.

    Positive power = consumption (Vessim's "absorbing"). At each instant the load has a
    reference power ``r`` (what it would draw without flexibility). It may run below ``r``
    while the backlog stays below ``max_backlog_wh`` and above ``r`` (up to
    ``max_catchup_w`` extra) while there is backlog to serve. The backlog has no deadline;
    whatever is left at the end of the run is unserved.

    ``feasible_range(duration)`` assumes ``r`` stays constant over ``duration``; for durations
    longer than one step it is therefore an estimate of the sustained flexibility.
    """

    def __init__(self, name: str, reference_kw: pd.Series, max_backlog_kwh: float,
                 max_catchup_kw: float) -> None:
        super().__init__(name)
        offs = (reference_kw.index - reference_kw.index[0]).total_seconds().to_numpy()
        self._offs, self._ref_w = offs, reference_kw.to_numpy() * 1e3
        self.max_backlog_wh, self.max_catchup_w = max_backlog_kwh * 1e3, max_catchup_kw * 1e3
        self.backlog_wh = 0.0
        self.elapsed_s = 0.0  # own clock: Dispatchables get no timestamp
        self.current_power = self.reference_w

    @property
    def reference_w(self) -> float:
        i = int(np.searchsorted(self._offs, self.elapsed_s, side="right")) - 1
        return float(self._ref_w[max(i, 0)])

    def feasible_range(self, duration: float) -> tuple[float, float]:
        r, dt_h = self.reference_w, duration / H
        lo = max(0.0, r - max(0.0, self.max_backlog_wh - self.backlog_wh) / dt_h)
        hi = r + min(self.max_catchup_w, self.backlog_wh / dt_h)
        return lo, hi

    def step(self, duration: float) -> None:
        self.backlog_wh += (self.reference_w - self.current_power) * duration / H
        self.backlog_wh = min(max(self.backlog_wh, 0.0), self.max_backlog_wh)  # float drift
        self.elapsed_s += duration

    def config(self) -> dict:
        return {"max_backlog_wh": self.max_backlog_wh, "max_catchup_w": self.max_catchup_w}

    def state(self) -> dict:
        return {"current_power": self.current_power, "reference_power": self.reference_w,
                "backlog_wh": self.backlog_wh}


class BalanceSlack(Dispatchable):
    """Takes the remaining imbalance: negative = unserved demand (shed), positive = spilled."""

    def feasible_range(self, duration: float) -> tuple[float, float]:
        return -math.inf, math.inf

    def step(self, duration: float) -> None:
        pass

    def config(self) -> dict:
        return {}

    def state(self) -> dict:
        return {"current_power": self.current_power}


# --------------------------------------------------------------------------------------
# Policy and controllers
# --------------------------------------------------------------------------------------


class GridServingPolicy(vs.DispatchPolicy):
    """Keep the grid within its feasible range using storage and deferrable load.

    Each interval, storages start idle and deferrable loads at their reference power.

    - Import above the grid's lower bound (``max_import`` or ramp): reduce absorption in
      ``shortfall_order`` ("battery" = storages, "flexible" = deferrable loads).
    - Export / ramp-up bound exceeded: increase absorption (charge, catch up).
    - Otherwise use the headroom below ``recovery_cap_kw`` (default: the grid limit) to catch
      up backlog first, then recharge storages while their SOC is below ``soc_target``.
    - Grid signal ``flex_activation`` −1 / +1 forces all resources to their minimum /
      maximum absorption (downward / upward flexibility activation).

    The grid takes what remains, clipped to its range; a :class:`BalanceSlack`, if present,
    takes the rest. Without a slack an infeasible step raises ``RuntimeError`` like
    Vessim's built-in policy. The grid connection must come after all other resources except
    the slack so that the balance closes exactly in floating point.
    """

    def __init__(self, shortfall_order: Sequence[str] = ("battery", "flexible"),
                 recovery_cap_kw: Optional[float] = None, use: Sequence[str] = ("battery", "flexible"),
                 activation_signal: str = "flex_activation", soc_target: float = 1.0) -> None:
        self.shortfall_order, self.use = tuple(shortfall_order), tuple(use)
        self.soc_target = soc_target
        self.recovery_cap_w = None if recovery_cap_kw is None else recovery_cap_kw * 1e3
        self.activation_signal = activation_signal
        self._mode = "normal"

    @staticmethod
    def _kind(d) -> str:
        return "battery" if isinstance(d, Storage) else "flexible" if isinstance(d, DeferrableLoad) else ""

    def apply(self, p_delta, duration, dispatchables, grid_signals=None) -> None:
        grid = [d for d in dispatchables if isinstance(d, GridConnection)]
        if len(grid) != 1:
            raise ValueError("GridServingPolicy expects exactly one GridConnection.")
        grid = grid[0]
        res = [d for d in dispatchables if self._kind(d)]
        rng = {d.name: d.feasible_range(duration) for d in res}
        p = {d.name: (d.reference_w if isinstance(d, DeferrableLoad) else 0.0) for d in res}
        # resources not in use are pinned to their reference
        movable = [d for d in res if self._kind(d) in self.use]
        glo, ghi = grid.feasible_range(duration)
        act = (grid_signals or {}).get(self.activation_signal, 0.0)
        g = p_delta - sum(p.values())  # grid power if resources stay at reference
        if act:
            self._mode = "activation down" if act < 0 else "activation up"
            for d in movable:
                p[d.name] = rng[d.name][0 if act < 0 else 1]
        elif g < glo:  # too much import -> absorb less
            self._mode = "limit"
            need = glo - g
            for kind in self.shortfall_order:
                for d in (d for d in movable if self._kind(d) == kind):
                    dec = min(need, p[d.name] - rng[d.name][0])
                    p[d.name] -= dec
                    need -= dec
        elif g > ghi:  # too much export / ramp-up bound -> absorb more
            self._mode = "absorb"
            need = g - ghi
            for d in movable:
                inc = min(need, rng[d.name][1] - p[d.name])
                p[d.name] += inc
                need -= inc
        else:  # headroom: catch up backlog, then recharge
            self._mode = "recover"
            cap = glo if self.recovery_cap_w is None else max(glo, -self.recovery_cap_w)
            head = max(0.0, g - cap)
            for kind in ("flexible", "battery"):
                for d in (d for d in movable if self._kind(d) == kind):
                    hi = rng[d.name][1]
                    if kind == "battery":
                        if d.soc() >= self.soc_target - 1e-9:
                            continue
                        if hasattr(d, "capacity_wh"):  # stop at the target (EfficiencyBattery)
                            hi = min(hi, (self.soc_target - d.soc()) * d.capacity_wh
                                     / (d.eff_ch * duration / H))
                    inc = max(0.0, min(head, hi - p[d.name]))
                    p[d.name] += inc
                    head -= inc
        # assign in list order; grid takes the remainder (clipped), slack the rest
        remaining, slack = p_delta, None
        for d in dispatchables:
            if d is grid or isinstance(d, BalanceSlack):
                slack = d if isinstance(d, BalanceSlack) else slack
                continue
            lo, hi = d.feasible_range(duration) if d.name not in rng else rng[d.name]
            setpoint = min(max(p.get(d.name, 0.0), lo), hi)
            d.set_power(setpoint, duration)
            remaining -= setpoint
        g = min(max(remaining, glo), ghi) if slack is not None else remaining
        if slack is None and not glo <= g <= ghi:
            reason = "Unmet demand" if g < glo else "Unabsorbed surplus"
            raise RuntimeError(f"{reason} of {abs(g - min(max(g, glo), ghi)):g} W: grid limit reached.")
        grid.set_power(g, duration)
        if slack is not None:
            slack.set_power(remaining - g, duration)

    def state(self) -> dict:
        return {"mode": self._mode}


class GridEventController(vs.Controller):
    """Apply import limit and connection state for the next interval from grid signals.

    Reads ``next_import_limit_w`` (< 0 = no limit) and ``next_connected`` (1 / 0) from the
    microgrid's grid signals. Controllers run after dispatch, so whatever is set here takes
    effect at the next dispatch — a limit must be announced one step ahead to apply on time.
    """

    def __init__(self, grid: GridConnection, microgrid: str = "site") -> None:
        self.grid, self.microgrid = grid, microgrid

    def step(self, now, microgrid_states) -> None:
        sig = microgrid_states[self.microgrid]["grid_signals"] or {}
        if "next_import_limit_w" in sig:
            v = sig["next_import_limit_w"]
            self.grid.max_import = None if v < 0 else v
        if "next_connected" in sig:
            self.grid.connected = bool(sig["next_connected"] >= 0.5)


class FlexibilityBand(vs.Controller):
    """Min / reference / max grid import per tick for several sustain durations (W).

    For each resource the downward flexibility is ``current − feasible_min(D)`` and the upward
    flexibility ``feasible_max(D) − current``. The band is clipped to the grid's power limits
    (import limit, connection). Ramp limits are not applied (they depend on the path). The
    observation needs object references: ``MicrogridState`` carries no feasible ranges.
    """

    def __init__(self, grid: GridConnection, resources: Sequence[Dispatchable],
                 durations_s: Sequence[int]) -> None:
        self.grid, self.resources, self.durations_s = grid, list(resources), list(durations_s)
        self.rows: list[dict] = []

    def step(self, now, microgrid_states) -> None:
        g = self.grid
        p_ref = -g.current_power  # import positive
        max_imp = 0.0 if not g.connected else (math.inf if g.max_import is None else g.max_import)
        max_exp = 0.0 if not g.connected else (math.inf if g.max_export is None else g.max_export)
        row = {"time": now, "p_ref_w": p_ref}
        for dur in self.durations_s:
            down = up = 0.0
            for d in self.resources:
                lo, hi = d.feasible_range(dur)
                down += max(0.0, d.current_power - lo)
                up += max(0.0, hi - d.current_power)
            row[f"p_min_w_{dur}"] = max(p_ref - down, -max_exp)
            row[f"p_max_w_{dur}"] = min(p_ref + up, max(max_imp, p_ref))
        self.rows.append(row)

    def to_df(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows).set_index("time")


# --------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------


@dataclass
class FlexParams:
    """Deferrable-load parameters (kWh / kW)."""

    max_backlog_kwh: float = 1000.0
    max_catchup_kw: float = 500.0


@dataclass
class GridSpec:
    """Grid connection settings (kW per step for ramps, as in Vessim)."""

    max_import_kw: Optional[float] = None
    max_export_kw: Optional[float] = None
    ramp_kw_per_step: Optional[float] = None  # symmetric ramp_up = ramp_down
    initial_import_kw: float = 0.0


@dataclass
class RunResult:
    ts: pd.DataFrame
    wall_s: float
    error: Optional[str] = None
    failed_at: Optional[pd.Timestamp] = None
    band: Optional[pd.DataFrame] = None
    attrs: dict = field(default_factory=dict)


def run_grid_site(
    site: FlexSite,
    battery: BatteryParams,
    policy: Literal["default"] | vs.DispatchPolicy = "default",
    order: Sequence[str] = ("battery", "grid"),
    grid: GridSpec = GridSpec(),
    flexible: Optional[FlexParams] = None,
    slack: bool = False,
    import_limit_kw: Optional[pd.Series] = None,
    connected: Optional[pd.Series] = None,
    announce: bool = True,
    activation: Optional[pd.Series] = None,
    band_durations_s: Sequence[int] = (),
    step_s: Optional[int] = None,
) -> RunResult:
    """Run the site once; errors raised by Vessim are caught and reported, not re-raised.

    ``order`` lists the dispatch order of "battery" and "grid" (built-in policy). With a
    custom policy the order is fixed: battery, flexible load, grid, slack.
    ``import_limit_kw`` (NaN = no limit) and ``connected`` (bool) give the state *effective*
    in each interval; with ``announce=True`` they are published one step ahead, otherwise in
    the same step (so the controller applies them one step late). Without a flexible
    dispatchable the deferrable share is added to the inflexible consumer.
    """
    step_s = step_s or site.step_s
    inflex = site.inflexible_kw if flexible else site.inflexible_kw + site.flexible_kw
    actors = [vs.Consumer("demand", signal=_trace_w(inflex)), vs.Producer("pv", signal=_trace_w(site.pv_kw))]
    bat = make_battery("losses", battery)
    ramp = None if grid.ramp_kw_per_step is None else grid.ramp_kw_per_step * 1e3
    lim0 = grid.max_import_kw
    if import_limit_kw is not None and not np.isnan(import_limit_kw.iloc[0]):
        lim0 = float(import_limit_kw.iloc[0])
    gc = vs.GridConnection(max_import=None if lim0 is None else lim0 * 1e3,
                           max_export=None if grid.max_export_kw is None else grid.max_export_kw * 1e3,
                           ramp_up=ramp, ramp_down=ramp, initial_power=-grid.initial_import_kw * 1e3,
                           connected=True if connected is None else bool(connected.iloc[0]))
    flex = (DeferrableLoad("flexible", site.flexible_kw, flexible.max_backlog_kwh,
                           flexible.max_catchup_kw) if flexible else None)
    if policy == "default":
        pol = vs.DefaultDispatchPolicy()
        dispatchables = [bat if k == "battery" else gc for k in order]
    else:
        pol = policy
        dispatchables = [bat] + ([flex] if flex else []) + [gc] + ([BalanceSlack("slack")] if slack else [])
    signals = {"price": _trace_w(site.price, scale=1.0)}
    shift = 1 if announce else 0
    if import_limit_kw is not None:
        nxt = import_limit_kw.fillna(-1.0).shift(-shift).ffill()  # NaN -> no limit (< 0)
        signals["next_import_limit_w"] = _trace_w(nxt.where(nxt < 0, nxt * 1e3), scale=1.0)
    if connected is not None:
        signals["next_connected"] = _trace_w(connected.astype(float).shift(-shift).ffill(), scale=1.0)
    if activation is not None:
        signals["flex_activation"] = _trace_w(activation, scale=1.0)

    env = vs.Environment(sim_start=site.start, step_size=step_s)
    env.add_microgrid(name="site", actors=actors, dispatchables=dispatchables, policy=pol,
                      grid_signals=signals)
    if import_limit_kw is not None or connected is not None:
        env.add_controller(GridEventController(gc))
    band = None
    if band_durations_s:
        band = env.add_controller(FlexibilityBand(gc, [d for d in dispatchables
                                                       if d is bat or d is flex], band_durations_s))
    rec = env.add_controller(vs.Recorder())
    error = failed_at = None
    t0 = time.perf_counter()
    try:
        env.run(until=site.horizon_s)
    except (RuntimeError, ValueError) as e:
        error = f"{type(e).__name__}: {e}"
    wall = time.perf_counter() - t0
    env.close()
    raw = rec.to_df()
    ts = _standardize(raw, step_s) if len(raw) else pd.DataFrame()
    if error:
        failed_at = (ts.index[-1] + pd.Timedelta(seconds=step_s)) if len(ts) else site.start
    if len(ts):
        ts["import_limit_kw"] = (import_limit_kw.reindex(ts.index, method="ffill")
                                 if import_limit_kw is not None
                                 else (np.nan if grid.max_import_kw is None else grid.max_import_kw))
        ts["price"] = site.price.reindex(ts.index, method="ffill")
    return RunResult(ts=ts, wall_s=wall, error=error, failed_at=failed_at,
                     band=_band_kw(band.to_df()) if band is not None and band.rows else None,
                     attrs={"step_s": step_s, "final_soc": bat.soc(),
                            "final_backlog_kwh": flex.backlog_wh / 1e3 if flex else 0.0})


def _standardize(raw: pd.DataFrame, step_s: int) -> pd.DataFrame:
    df = raw.xs("site", level="microgrid")
    out = pd.DataFrame(index=pd.DatetimeIndex(df.index, name="time"))
    col = lambda k: df[k].to_numpy() if k in df else np.zeros(len(df))  # noqa: E731
    out["grid_kw"] = -col("p_grid") / 1e3
    out["battery_kw"] = -col("dispatch_states.battery.current_power") / 1e3
    out["soc"] = col("dispatch_states.battery.soc")
    out["pv_kw"] = col("actor_states.pv.power") / 1e3
    out["inflexible_kw"] = -col("actor_states.demand.power") / 1e3
    out["flexible_kw"] = col("dispatch_states.flexible.current_power") / 1e3
    out["flexible_ref_kw"] = col("dispatch_states.flexible.reference_power") / 1e3
    out["backlog_kwh"] = col("dispatch_states.flexible.backlog_wh") / 1e3
    out["unserved_kw"] = (-col("dispatch_states.slack.current_power") / 1e3).clip(min=0)
    out["spilled_kw"] = (col("dispatch_states.slack.current_power") / 1e3).clip(min=0)
    out["connected"] = df["dispatch_states.grid.connected"].to_numpy().astype(bool)
    out["mode"] = df["policy_state.mode"].to_numpy() if "policy_state.mode" in df else ""
    out.attrs["step_s"] = step_s
    return out


def _band_kw(b: pd.DataFrame) -> pd.DataFrame:
    return b / 1e3


# --------------------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------------------


def grid_kpis(r: RunResult, window: Optional[tuple[str, str]] = None,
              limit_kw: Optional[float] = None) -> pd.Series:
    """KPIs of one run; ``window`` ('HH:MM', 'HH:MM') restricts the event KPIs."""
    ts, dt_h = r.ts, r.attrs["step_s"] / H
    out = {"Completed": r.error is None,
           "Failed at": r.failed_at.strftime("%H:%M") if r.failed_at is not None else ""}
    if not len(ts):
        return pd.Series(out)
    lim = ts["import_limit_kw"]
    over = (ts["grid_kw"] - lim).clip(lower=0).fillna(0)
    out.update({
        "Peak grid import (kW)": ts["grid_kw"].max(),
        "Grid import energy (kWh)": ts["grid_kw"].clip(lower=0).sum() * dt_h,
        "Energy cost (€)": (ts["grid_kw"].clip(lower=0) * ts["price"]).sum() * dt_h / 1e3,
        "Import above limit (kWh)": over.sum() * dt_h,
        "Unserved energy (kWh)": ts["unserved_kw"].sum() * dt_h,
        "Battery discharge (kWh)": ts["battery_kw"].clip(lower=0).sum() * dt_h,
        "Flexible load deferred (kWh)": (ts["flexible_ref_kw"] - ts["flexible_kw"]).clip(lower=0).sum() * dt_h,
        "Backlog left at end (kWh)": r.attrs.get("final_backlog_kwh", 0.0),
        "Final state of charge (%)": 100 * r.attrs["final_soc"],
    })
    if window is not None:
        tod = ts.index - ts.index.normalize()
        a, b = pd.Timedelta(window[0] + ":00"), pd.Timedelta(window[1] + ":00")
        ev, post = ts[(tod >= a) & (tod < b)], ts[(tod >= b) & (tod < b + pd.Timedelta(hours=2))]
        out.update({
            "Max import in window (kW)": ev["grid_kw"].max(),
            "Max import in 2 h after window (kW)": post["grid_kw"].max(),
        })
    return pd.Series(out)


def first_error(r: RunResult) -> str:
    """Vessim's error message without the exception type (empty if the run completed)."""
    return "" if r.error is None else r.error.split(":", 1)[1].strip()


def lowest_feasible(run: Callable[[float], RunResult], lo: float, hi: float,
                    tol: float = 5.0) -> float:
    """Smallest value in [lo, hi] for which ``run(value)`` completes (bisection).

    Assumes feasibility is monotonic in the value (looser limit = larger value). Returns
    ``nan`` if even ``hi`` fails.
    """
    if run(hi).error is not None:
        return float("nan")
    while hi - lo > tol:
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if run(mid).error is None else (mid, hi)
    return hi


def activation_check(run: Callable[[Optional[pd.Series]], RunResult], base: RunResult,
                     starts: Sequence[str], duration_s: int, compare_s: int) -> pd.DataFrame:
    """Activate the band edge for ``duration_s`` from each start and compare with the band.

    ``run(activation)`` re-runs the baseline with a ``flex_activation`` signal (−1 down,
    +1 up). Delivered = mean change of grid import over the window vs ``base``. Predicted =
    band edge minus reference at the start, for ``duration_s`` and for ``compare_s``.
    """
    idx, rows = base.ts.index, {}
    for t0 in starts:
        a = idx[0].normalize() + pd.Timedelta(t0 + ":00")
        b = a + pd.Timedelta(seconds=duration_s)
        win = (idx >= a) & (idx < b)
        band = base.band.loc[a]
        for sign, label, edge in ((-1, "down", "p_min_w"), (1, "up", "p_max_w")):
            act = pd.Series(np.where(win, sign, 0.0), index=idx)
            r = run(act)
            rows[(t0, label)] = {
                "Delivered, mean change of grid import (kW)": (r.ts.grid_kw - base.ts.grid_kw)[win].mean(),
                f"Predicted, {duration_s // 60}-min band (kW)": band[f"{edge}_{duration_s}"] - band.p_ref_w,
                f"Predicted, {compare_s // 60}-min band (kW)": band[f"{edge}_{compare_s}"] - band.p_ref_w,
                "Unserved energy (kWh)": r.ts.unserved_kw.sum() * r.attrs["step_s"] / H,
            }
    out = pd.DataFrame(rows).T
    out.index.names = ["Start", "Direction"]
    return out


# --------------------------------------------------------------------------------------
# Edge study flexibility band
# --------------------------------------------------------------------------------------


def extract_edge_band(flex_csv: Path, start: str, out_csv: Path, step_s: int = 900) -> pd.DataFrame:
    """Aggregate the edge study's ``flexibility_results.csv`` to a regular step (kW).

    Keeps horizon step 0 (the band anchored on the observed grid power). Per step: means of
    p_ref and of the band offsets (job, MPC battery, offline-optimal battery), and the
    p_ref forecast for horizon step 1 (next scheduler call) to measure its offset.
    """
    f = pd.read_csv(flex_csv)
    f["time"] = pd.Timestamp(start) + pd.to_timedelta(f["time"], unit="s")
    f0 = f[f.horizon_step == 0].set_index("time")
    out = pd.DataFrame({
        "p_ref_kw": f0.p_grid_ref,
        "job_down_kw": f0.p_grid_ref - f0.p_grid_min,
        "job_up_kw": f0.p_grid_max - f0.p_grid_ref,
        "bat_down_kw": f0.p_grid_ref - f0.p_grid_min_bat,
        "bat_up_kw": f0.p_grid_max_bat - f0.p_grid_ref,
        "batopt_down_kw": f0.p_grid_ref - f0.p_grid_min_bat_opt,
        "batopt_up_kw": f0.p_grid_max_bat_opt - f0.p_grid_ref,
    })
    f1 = f[f.horizon_step == 1].set_index("time")
    out["p_ref_h1_kw"] = f1.p_grid_ref
    agg = out.resample(f"{step_s}s").mean()
    agg["n_calls"] = out["p_ref_kw"].resample(f"{step_s}s").count()
    agg = agg.round(3)
    agg.to_csv(out_csv)
    return agg


__all__ = ["FlexSite", "window_series", "DeferrableLoad", "BalanceSlack", "GridServingPolicy",
           "GridEventController", "FlexibilityBand", "FlexParams", "GridSpec", "RunResult",
           "run_grid_site", "grid_kpis", "first_error", "lowest_feasible", "activation_check",
           "extract_edge_band"]
