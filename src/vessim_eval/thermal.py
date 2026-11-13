"""Cooling, room temperature and waste heat retrofitted onto Vessim (E04).

Vessim has no thermal concepts: no heat flows, temperatures, cooling chain or heat recovery,
and actors cannot read grid signals such as an outdoor temperature. This module adds them
through Vessim's ``Actor`` extension point:

- :class:`CoolingChain` — quasi-steady port of the edge study's cooling chain in heat mode
  ``none`` (``simulators/cooling_crah.py``, ``cooling_chiller.py``, ``pump_dc.py``,
  ``pump_chiller_evap.py``): CRAH fan (cubic in air flow), trim chiller with a Carnot COP
  that depends on the outdoor temperature, and two pumps (linear in water flow).
- :class:`PowerProbe` — a trace consumer that remembers its last power so that actors listed
  after it can use it as a heat input (Vessim samples actors in list order).
- :class:`ChainCooling` — electrical power of the cooling chain for the heat of another actor
  and an outdoor-temperature trace held by the actor itself.
- :class:`RoomThermal` — one lumped room-air node (thermal capacity, no losses) with a
  proportional cooling controller, optional precooling and curtailment windows, and a
  temperature override that can only act at sample instants.
- :class:`HeatRecovery` — a heat pump that lifts IT waste heat to a district-heating supply
  temperature. Its heat output exists only in the actor's state dict; Vessim has no place
  for heat in its balance.

Units: kW, °C and seconds in public helpers, W inside Vessim. Consumer power is negative.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np
import pandas as pd
import vessim as vs

from vessim_eval.scenarios import _trace_w

# Edge study planning assumption (simulators/controller.py, scheduler.py): 0.95 × 0.3.
EDGE_COOLING_OVERHEAD = 0.95 * 0.3

# --------------------------------------------------------------------------------------
# Edge reference extraction
# --------------------------------------------------------------------------------------

THERMAL_COLUMNS = {
    "hardware_power": "it_kw",
    "ambient_temp": "ambient_temp_c",
    "cooling_power": "cooling_kw",
    "chiller_power_w": "chiller_kw",
    "crah_fan_power_w": "fan_kw",
    "pump_dc_power_w": "pump_dc_kw",
    "pump_chiller_evap_power_w": "pump_evap_kw",
    "crah_heat_removed_w": "heat_removed_kw",
    "heatpump_power": "heat_pump_kw",
    "hp_cop": "heat_pump_cop",
    "tank_mean_temp_c": "tank_mean_c",
    "tank_top_temp_c": "tank_top_c",
    "heat_demand": "heat_demand_kw",
    "heating_network_in": "heat_to_network_kw",
    "dh_hx_heat_output_w": "dh_hx_heat_kw",
    "dbg_tank_protection": "tank_protection",
    "dbg_hp_in_src_T": "hp_source_in_c",
    "dbg_hp_out_src_T": "hp_source_out_c",
}


def extract_thermal_reference(results_dir: Path, dc_id: str, out_csv: Path,
                              step_s: int = 900, extra: Optional[dict] = None) -> pd.DataFrame:
    """Cooling-chain columns of an edge run at the start of each ``step_s`` bin (kW, °C).

    The edge cooling chain steps every physical step (15 min) and holds its outputs, so the
    value at the start of a bin is the value of the whole bin. ``it_mean_kw`` is the
    time-weighted IT power over the bin. Watt columns are converted to kW.
    """
    from vessim_eval.edge_reference import _resample

    results_dir = Path(results_dir)
    cfg = json.loads((results_dir / "scenario_config.json").read_text())
    start = pd.Timestamp(cfg["simulation"]["start_datetime"])
    horizon_s = int(cfg["simulation"]["duration_seconds"])
    raw = pd.read_csv(results_dir / "measurements_results.csv")
    columns = {**THERMAL_COLUMNS, **(extra or {})}
    rename = {f"{k}_{dc_id}": v for k, v in columns.items() if f"{k}_{dc_id}" in raw}
    sampled, mean = _resample(raw[["time", *rename]].rename(columns=rename), horizon_s, step_s)
    for key, col in columns.items():  # edge columns ending in "_w" are in W
        if key.endswith("_w") and col in sampled:
            sampled[col] = sampled[col] / 1e3
    sampled.insert(1, "it_mean_kw", mean["it_kw"])
    sampled.index = start + pd.to_timedelta(sampled.index, unit="s")
    sampled.index.name = "time"
    sampled = sampled.loc[:, sampled.abs().sum() > 0]  # drop columns unused in this mode
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    sampled.round(6).to_csv(out_csv)
    return sampled


def heat_demand_kw(ambient_c, annual_heat_kwh: float = 29_238_000,
                   annual_hot_water_kwh: float = 690_000, t_limit_c: float = 15.0,
                   t_design_c: float = -12.0, degree_hours: float = 74_000):
    """District-heating demand (kW) of the edge study (``simulators/heating_network.py``).

    Space heating ramps linearly from 0 at the heating limit to its peak at the design
    temperature; hot water is a constant base load. Defaults: scenario S2.2.
    """
    p_peak = (annual_heat_kwh - annual_hot_water_kwh) * (t_limit_c - t_design_c) / degree_hours
    t = np.asarray(ambient_c, dtype=float)
    return p_peak * np.maximum(0.0, (t_limit_c - t) / (t_limit_c - t_design_c)) \
        + annual_hot_water_kwh / 8760.0


# --------------------------------------------------------------------------------------
# Cooling chain (quasi-steady port of the edge study, heat mode "none")
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CoolingChain:
    """Edge study cooling chain with its default parameters (S1.2).

    The edge study connects no room-air model to the CRAH, so its return-air temperature is
    the constant default (30 °C), and the chiller holds the chilled-water supply at its set
    point. Under these conditions the chain is algebraic in (heat, outdoor temperature).
    """

    # Trim chiller (cooling_chiller.py)
    eta_carnot: float = 0.40
    cop_min: float = 1.5
    cop_max: float = 4.5
    evap_approach_k: float = 2.0
    cond_approach_k: float = 10.0
    t_cond_min_c: float = 20.0
    t_supply_c: float = 15.0
    # CRAH (cooling_crah.py)
    t_air_return_c: float = 30.0
    t_air_supply_c: float = 20.0
    air_flow_design_m3_s: float = 414.59
    fan_design_kw: float = 120.0
    water_dt_k: float = 5.0
    # Pumps (pump_dc.py, pump_chiller_evap.py): pressure rise (Pa) and efficiency
    pump_dc_pa: float = 200_000.0
    pump_evap_pa: float = 80_000.0
    pump_eta: float = 0.75

    @property
    def capacity_kw(self) -> float:
        """Heat the CRAH can remove at design air flow (kW)."""
        return 1.2 * 1005.0 * self.air_flow_design_m3_s \
            * (self.t_air_return_c - self.t_air_supply_c) / 1e3

    def cop(self, ambient_c):
        """Chiller COP: Carnot COP × efficiency, clipped to [cop_min, cop_max]."""
        t_evap = self.t_supply_c - self.evap_approach_k + 273.15
        t_cond = np.maximum(np.asarray(ambient_c, dtype=float) + self.cond_approach_k,
                            self.t_cond_min_c) + 273.15
        return np.clip(self.eta_carnot * t_evap / (t_cond - t_evap), self.cop_min, self.cop_max)

    def power_kw(self, heat_kw, ambient_c, chiller_heat_kw=None) -> dict:
        """Electrical power (kW) of each component to remove ``heat_kw`` (arrays allowed).

        The CRAH fan and the data-center pump move all of ``heat_kw``; the chiller and its
        evaporator pump only ``chiller_heat_kw`` (default: all of it).
        """
        q = np.clip(np.asarray(heat_kw, dtype=float), 0.0, self.capacity_kw)
        q_ch = q if chiller_heat_kw is None else np.clip(np.asarray(chiller_heat_kw, float), 0, q)
        air_ratio = q * 1e3 / (1.2 * 1005.0 * (self.t_air_return_c - self.t_air_supply_c)) \
            / self.air_flow_design_m3_s
        water_m3_s = q * 1e3 / (4180.0 * self.water_dt_k) / 1000.0
        cop = self.cop(ambient_c)
        out = {
            "heat_removed_kw": q,
            "chiller_kw": q_ch / cop,
            "fan_kw": self.fan_design_kw * air_ratio ** 3,
            "pump_dc_kw": self.pump_dc_pa * water_m3_s / self.pump_eta / 1e3,
            "pump_evap_kw": self.pump_evap_pa * water_m3_s * q_ch / np.maximum(q, 1e-9)
            / self.pump_eta / 1e3,
            "cop": cop,
        }
        out["total_kw"] = out["chiller_kw"] + out["fan_kw"] + out["pump_dc_kw"] \
            + out["pump_evap_kw"]
        return out


# --------------------------------------------------------------------------------------
# Vessim actors
# --------------------------------------------------------------------------------------


def _trace(s: pd.Series) -> vs.Trace:
    """Unscaled offset-indexed Trace (for temperatures)."""
    return _trace_w(s, scale=1.0)


class PowerProbe(vs.Consumer):
    """Trace consumer that remembers its last sampled power (kW) for later actors."""

    def __init__(self, name: str, signal):
        super().__init__(name, signal=signal)
        self.last_power_kw = 0.0

    def sample(self, elapsed):
        power, state = super().sample(elapsed)
        self.last_power_kw = -power / 1e3
        return power, state


class ChainCooling(vs.Consumer):
    """Cooling-chain electricity for the heat reported by ``heat_kw()`` (kW).

    Actors cannot read grid signals, so the outdoor temperature is a Trace held here.
    Must be listed after the actor that ``heat_kw`` reads.
    """

    def __init__(self, name: str, heat_kw: Callable[[], float], ambient_c: pd.Series,
                 chain: CoolingChain = CoolingChain(),
                 chiller_heat_kw: Optional[Callable[[], float]] = None):
        super().__init__(name, signal=0.0)
        self.heat_kw, self.ambient, self.chain = heat_kw, _trace(ambient_c), chain
        self.chiller_heat_kw = chiller_heat_kw

    def sample(self, elapsed):
        ch = self.chiller_heat_kw() if self.chiller_heat_kw else None
        p = self.chain.power_kw(self.heat_kw(), self.ambient.at(elapsed), ch)
        power = -float(p["total_kw"]) * 1e3
        state = {k: float(v) for k, v in p.items() if k != "total_kw"}
        return power, {"power": power, **state}


@dataclass(frozen=True)
class Window:
    """A time window (seconds since start) with a temporary set point or curtailment."""

    start_s: int
    end_s: int
    setpoint_c: Optional[float] = None       # precooling set point
    max_removal_frac: Optional[float] = None  # cap on heat removal, fraction of IT heat


class RoomThermal(vs.Consumer):
    """Lumped room-air node cooled by the cooling chain.

    At each sample ``t`` the actor (1) integrates the room temperature over the last interval
    with the heat input and removal held since the previous sample (exact for constant
    powers), and (2) chooses the heat removal for ``[t, t + step)``:
    proportional control to the set point with time constant ``tau_s``, capped by the CRAH
    capacity and, inside a curtailment window, by a fraction of the IT heat. If the room is
    at or above ``t_max_c`` the curtailment is overridden — but only at sample instants, so the
    room can overshoot by up to one step of unbalanced heat.
    """

    def __init__(self, name: str, heat_kw: Callable[[], float], ambient_c: pd.Series,
                 capacity_mj_k: float = 240.0, t_set_c: float = 24.0, t_max_c: float = 27.0,
                 tau_s: float = 900.0, windows: Sequence[Window] = (),
                 chain: CoolingChain = CoolingChain()):
        super().__init__(name, signal=0.0)
        self.heat_kw, self.ambient, self.chain = heat_kw, _trace(ambient_c), chain
        self.c_kj_k = capacity_mj_k * 1e3
        self.t_set, self.t_max, self.tau, self.windows = t_set_c, t_max_c, tau_s, windows
        self.t_room = t_set_c
        self.t_peak = t_set_c                 # highest temperature reached in the last interval
        self._last = None                     # (elapsed, heat_kw, removal_kw)

    def _window(self, elapsed: float) -> Optional[Window]:
        return next((w for w in self.windows if w.start_s <= elapsed < w.end_s), None)

    def sample(self, elapsed):
        if self._last is not None:
            t0, q_in, q_out = self._last
            self.t_room += (q_in - q_out) * (elapsed - t0) / self.c_kj_k
        self.t_peak = self.t_room
        heat = self.heat_kw()
        w = self._window(elapsed)
        t_set = w.setpoint_c if w and w.setpoint_c is not None else self.t_set
        removal = heat + self.c_kj_k * (self.t_room - t_set) / self.tau
        curtailed = bool(w and w.max_removal_frac is not None and self.t_room < self.t_max)
        if curtailed:
            removal = min(removal, w.max_removal_frac * heat)
        removal = float(np.clip(removal, 0.0, self.chain.capacity_kw))
        self._last = (elapsed, heat, removal)
        p = self.chain.power_kw(removal, self.ambient.at(elapsed))
        power = -float(p["total_kw"]) * 1e3
        return power, {"power": power, "room_c": self.t_room, "heat_removed_kw": removal,
                       "curtailed": float(curtailed), "setpoint_c": t_set}


@dataclass(frozen=True)
class HeatPumpParams:
    """Heat-recovery heat pump (edge ``simulators/heat_pump.py``; S2.2 sizes)."""

    eta_carnot: float = 0.48
    cop_min: float = 1.1
    cop_max: float = 8.5
    capacity_kw: float = 5000.0      # condenser heat (Q_cond_nom_W)
    compressor_max_kw: float = 600.0  # P_comp_max_W (default, not set in S2.2)
    delivery_cap_kw: float = np.inf  # heat the district-heating exchanger can pass on
    t_sink_c: float = 55.0           # district-heating supply
    t_source_c: float = 29.0         # mean source temperature (S2.2: ≈ 36 °C in, 22 °C out)

    @property
    def cop(self) -> float:
        lift = max(0.1, self.t_sink_c - self.t_source_c)
        return float(np.clip(self.eta_carnot * (self.t_sink_c + 273.15) / lift,
                             self.cop_min, self.cop_max))


class HeatRecovery(vs.Consumer):
    """Heat pump serving a district-heating demand from IT waste heat.

    Condenser heat = min(demand, capacity, heat obtainable from the waste heat). The waste
    heat it does not use is exposed as ``residual_heat_kw`` for a cooling actor listed after
    it. The delivered heat appears only in the state dict: Vessim balances electricity only.
    """

    def __init__(self, name: str, heat_kw: Callable[[], float], demand_kw: pd.Series,
                 hp: HeatPumpParams = HeatPumpParams()):
        super().__init__(name, signal=0.0)
        self.heat_kw, self.demand, self.hp = heat_kw, _trace(demand_kw), hp
        self.residual_heat_kw = 0.0

    def sample(self, elapsed):
        cop, waste = self.hp.cop, max(0.0, self.heat_kw())
        demand = self.demand.at(elapsed)
        q_cond = min(demand, self.hp.capacity_kw, self.hp.delivery_cap_kw,
                     self.hp.compressor_max_kw * cop, waste * cop / (cop - 1.0))
        p_el = q_cond / cop
        self.residual_heat_kw = waste - (q_cond - p_el)
        power = -p_el * 1e3
        return power, {"power": power, "heat_delivered_kw": q_cond, "heat_demand_kw": demand,
                       "waste_heat_used_kw": q_cond - p_el, "cop": cop}


class DeliveredHeat(vs.Metric):
    """Integral of a heat flow that an actor reports in its state (kWh, or € with a price).

    Vessim has no heat balance; a custom metric can still read actor states each tick.
    """

    def __init__(self, microgrid: str, actor: str, key: str = "heat_delivered_kw",
                 price_eur_per_kwh: Optional[float] = None):
        super().__init__("EUR" if price_eur_per_kwh else "kWh", microgrid=microgrid,
                         actor=actor, description=f"Integral of actor state '{key}'",
                         assumptions={"price_eur_per_kwh": price_eur_per_kwh})
        self.key, self.price = key, price_eur_per_kwh or 1.0

    def step(self, now, states, duration):
        heat_kw = states[self.microgrid]["actor_states"][self.actor][self.key]
        self.value += heat_kw * duration / 3600 * self.price


# --------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------

CoolingModel = str  # "constant_overhead" | "linear_fit" | "chain" | "room" | "heat_recovery"


def run_thermal(
    it_kw: pd.Series,
    ambient_c: pd.Series,
    model: CoolingModel,
    step_s: int = 900,
    building_kw: Optional[pd.Series] = None,
    pv_kw: Optional[pd.Series] = None,
    chain: CoolingChain = CoolingChain(),
    linear: tuple[float, float] = (EDGE_COOLING_OVERHEAD, 0.0),
    room: Optional[dict] = None,
    heat_demand: Optional[pd.Series] = None,
    hp: HeatPumpParams = HeatPumpParams(),
    heat_price_eur_per_kwh: float = 0.08,
) -> dict:
    """Run IT load + one cooling model (+ building and PV if given), grid only.

    Returns ``{"ts", "wall_s", "step_s", "metrics"}``; ``metrics`` holds the delivered heat
    (kWh) and its value (€) from :class:`DeliveredHeat` for the heat-recovery model. ``ts`` has kW columns ``it_kw``, ``cooling_kw``,
    ``grid_kw`` (+ = import), optional ``heat_pump_kw``, and every extra state the actors
    report, named ``<actor>_<key>``.
    """
    from vessim_eval.workload import CoolingProxy

    it = PowerProbe("it", _trace_w(it_kw))
    actors: list[vs.Actor] = [it]
    if model == "constant_overhead":
        actors.append(vs.Consumer("cooling", signal=_trace_w(linear[0] * it_kw)))
    elif model == "linear_fit":
        actors.append(CoolingProxy("cooling", it, *linear))
    elif model == "chain":
        actors.append(ChainCooling("cooling", lambda: it.last_power_kw, ambient_c, chain))
    elif model == "room":
        actors.append(RoomThermal("cooling", lambda: it.last_power_kw, ambient_c, chain=chain,
                                  **(room or {})))
    elif model == "heat_recovery":
        hr = HeatRecovery("heat_pump", lambda: it.last_power_kw, heat_demand, hp)
        actors += [hr, ChainCooling("cooling", lambda: it.last_power_kw, ambient_c, chain,
                                    chiller_heat_kw=lambda: hr.residual_heat_kw)]
    else:
        raise ValueError(f"Unknown cooling model {model!r}")
    if building_kw is not None:
        actors.append(vs.Consumer("building", signal=_trace_w(building_kw)))
    if pv_kw is not None:
        actors.append(vs.Producer("pv", signal=_trace_w(pv_kw)))

    env = vs.Environment(sim_start=it_kw.index[0], step_size=step_s)
    env.add_microgrid(name="site", actors=actors, dispatchables=[vs.GridConnection()])
    custom = {}
    if model == "heat_recovery":
        custom = {"Heat delivered (kWh)": DeliveredHeat("site", "heat_pump"),
                  "Heat revenue (€)": DeliveredHeat("site", "heat_pump",
                                                    price_eur_per_kwh=heat_price_eur_per_kwh)}
    rec = env.add_controller(vs.Recorder(custom_metrics=custom))
    horizon_s = int((it_kw.index[-1] - it_kw.index[0]).total_seconds()) \
        + int((it_kw.index[1] - it_kw.index[0]).total_seconds())
    t0 = time.perf_counter()
    env.run(until=horizon_s)
    wall = time.perf_counter() - t0
    env.close()

    df = rec.to_df().xs("site", level="microgrid")
    ts = pd.DataFrame(index=pd.DatetimeIndex(df.index, name="time"))
    for col in df.columns:
        if col.startswith("actor_states."):
            _, actor, key = col.split(".", 2)
            name = f"{actor}_kw" if key == "power" else f"{actor}_{key}"
            values = pd.to_numeric(df[col], errors="coerce").to_numpy()
            ts[name] = -values / 1e3 if key == "power" else values
    ts["grid_kw"] = -df["p_grid"].to_numpy() / 1e3
    if "pv_kw" in ts:
        ts["pv_kw"] = -ts["pv_kw"]
    metrics = {k: rec.metrics["actors"]["site"]["heat_pump"][k] for k in custom}
    return {"ts": ts, "wall_s": wall, "step_s": step_s, "metrics": metrics}


def energy_kwh(s: pd.Series, step_s: int) -> float:
    """Energy (kWh) of a step-held power series (kW)."""
    return float(s.sum() * step_s / 3600)


def cooling_agreement(reference_kw: pd.Series, candidate_kw: pd.Series, step_s: int) -> pd.Series:
    """Energy, peak and error statistics of a cooling power series against a reference."""
    ref, cand = reference_kw.align(candidate_kw, join="inner")
    diff = cand - ref
    return pd.Series({
        "Cooling energy (kWh)": energy_kwh(cand, step_s),
        "Energy difference to edge study (kWh)": energy_kwh(diff, step_s),
        "Energy difference to edge study (%)": 100 * diff.sum() / ref.sum(),
        "Mean absolute difference (kW)": float(diff.abs().mean()),
        "Max absolute difference (kW)": float(diff.abs().max()),
        "Peak cooling power (kW)": float(cand.max()),
    })


def curtailment_kpis(run: dict, baseline_kw: pd.Series, price: pd.Series, event: Window,
                     t_max_c: float) -> pd.Series:
    """Room temperature and cooling-energy effect of a curtailment event (15-min means).

    ``baseline_kw`` is the cooling power without the event at 15-min resolution; ``price`` in
    €/MWh. Room temperatures are taken at sample instants (the trajectory is piecewise linear
    between them, so the sampled maximum is the true maximum).
    """
    ts, step_s = run["ts"], run["step_s"]
    d = ts.cooling_kw.resample("15min").mean() - baseline_kw
    start = ts.index[0]
    in_event = (d.index >= start + pd.Timedelta(seconds=event.start_s)) \
        & (d.index < start + pd.Timedelta(seconds=event.end_s))
    p = price.reindex(d.index, method="ffill")
    return pd.Series({
        "Max room temperature (°C)": ts.cooling_room_c.max(),
        "Time above the limit (min)": (ts.cooling_room_c > t_max_c + 1e-6).sum() * step_s / 60,
        "Cooling energy saved in the event window (kWh)": -energy_kwh(d[in_event], 900),
        "Extra cooling energy outside the window (kWh)": energy_kwh(d[~in_event], 900),
        "Net change in cooling energy (kWh)": energy_kwh(d, 900),
        "Largest cooling reduction, 15-min mean (kW)": -d.min(),
        "Largest rebound, 15-min mean (kW)": d.max(),
        "Change in energy cost (€)": float((d * p / 1000).sum() * 0.25),
    })


def heat_kpis(heat_pump_kw: pd.Series, condenser_kw: pd.Series, delivered_kw: pd.Series,
              demand_kw: pd.Series, chiller_kw: pd.Series, cooling_kw: pd.Series,
              step_s: int = 900, heat_price_eur_per_kwh: float = 0.08) -> pd.Series:
    """Daily heat-recovery KPIs (kWh, %, €) from power series in kW."""
    e = lambda s: energy_kwh(s, step_s)  # noqa: E731
    return pd.Series({
        "Heat-pump electricity (kWh)": e(heat_pump_kw),
        "Heat-pump condenser heat (kWh)": e(condenser_kw),
        "Heat delivered to the network (kWh)": e(delivered_kw),
        "Heat demand met (%)": 100 * e(delivered_kw) / e(demand_kw),
        "Chiller electricity (kWh)": e(chiller_kw),
        "Cooling electricity incl. chiller (kWh)": e(cooling_kw),
        "Cooling + heat-pump electricity (kWh)": e(cooling_kw) + e(heat_pump_kw),
        "Heat revenue (€)": e(delivered_kw) * heat_price_eur_per_kwh,
    })
