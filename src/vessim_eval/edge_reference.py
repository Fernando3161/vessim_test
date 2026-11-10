"""Access to edge data center study reference runs.

The edge study runs in its own environment (``external/.venv-edge``, Python 3.12)
because its pinned dependencies conflict with ``.venv``. A reference run is done
once with :func:`run_edge_scenario`; :func:`extract_reference` resamples the
columns needed for comparison to a regular grid and writes a small CSV to ``data/``.

Columns ``*_kw`` are sampled at the start of each step (the edge study's own grid
accounting holds these samples for the whole step); ``*_mean_kw`` are time-weighted
means over the step.

Edge study conventions (kept in the extracted CSV, all kW):
``grid_import_kw`` >= 0 is grid import, ``battery_out_kw`` > 0 is battery discharge
(the only battery power the edge recorder logs), ``battery_charge_kw`` is derived from
the demand balance, ``battery_net_out_kw`` = discharge - charge,
``lp_battery_setpoint_kw`` > 0 is discharge in the offline-optimal LP schedule.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from vessim_eval import DATA_DIR, EXTERNAL_DIR

EDGE_DIR = EXTERNAL_DIR / "edge-dc-study"
EDGE_PYTHON = EXTERNAL_DIR / ".venv-edge" / ("Scripts/python.exe" if sys.platform == "win32"
                                             else "bin/python")

# Edge recorder column prefix (``_<dc_id>`` suffix) -> standard column name
REFERENCE_COLUMNS = {
    "hardware_power": "it_kw",
    "building_power": "building_kw",
    "cooling_power": "cooling_kw",
    "pv_power": "pv_kw",
    "battery_power": "battery_out_kw",
    "battery_soc": "soc",
    "grid_power": "grid_import_kw",
    "total_supply": "total_supply_kw",
    "total_demand": "total_demand_kw",
    "ambient_temp": "ambient_temp_c",
}


def scenario_config(scenario: str) -> dict:
    path = EDGE_DIR / "config" / "scenarios" / scenario / "simulation_config.json"
    return json.loads(path.read_text())


def run_edge_scenario(scenario: str, run_name: str, timeout_s: int = 900) -> Path:
    """Run one edge study scenario in its own environment; return its results dir."""
    config = EDGE_DIR / "config" / "scenarios" / scenario / "simulation_config.json"
    cmd = [str(EDGE_PYTHON), "scenario.py", "--name", run_name, "--config", str(config)]
    subprocess.run(cmd, cwd=EDGE_DIR, check=True, timeout=timeout_s)
    return EDGE_DIR / "results" / run_name


def _resample(raw: pd.DataFrame, horizon_s: int, step_s: int,
              base_s: int = 30) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (sampled, time-weighted mean) per ``step_s`` bin.

    Edge results are event-based (rows whenever any simulator stepped). Values are
    held until the next event. ``sampled`` is the value at the start of each bin,
    which is what the edge study's energy supply / grid accounting uses;
    ``mean`` is the time-weighted average over the bin (energy-conserving).
    """
    grid = np.arange(0, horizon_s, base_s)
    held = raw.set_index("time").sort_index()
    held = held[~held.index.duplicated(keep="last")]
    held = held.reindex(held.index.union(grid)).ffill().loc[grid]
    bins = (held.index // step_s) * step_s
    return held.groupby(bins).first(), held.groupby(bins).mean()


def extract_reference(results_dir: Path, dc_id: str, out_csv: Path, step_s: int = 900) -> pd.DataFrame:
    """Resample comparison columns of an edge run to ``step_s`` and save them to ``out_csv``.

    Also merges the offline-optimal LP battery schedule (``battery_optimal_results.csv``).
    """
    results_dir = Path(results_dir)
    cfg = json.loads((results_dir / "scenario_config.json").read_text())
    start = pd.Timestamp(cfg["simulation"]["start_datetime"])
    horizon_s = int(cfg["simulation"]["duration_seconds"])

    raw = pd.read_csv(results_dir / "measurements_results.csv")
    rename = {f"{k}_{dc_id}": v for k, v in REFERENCE_COLUMNS.items() if f"{k}_{dc_id}" in raw}
    sampled, mean = _resample(raw[["time", *rename]].rename(columns=rename), horizon_s, step_s)
    df = sampled
    for col in ("it_kw", "building_kw", "cooling_kw", "pv_kw"):
        if col in mean:
            df[col.replace("_kw", "_mean_kw")] = mean[col]
    # The edge recorder logs only battery discharge (e_el_out). Charging power enters
    # the energy supply as demand, so recover it from the demand balance.
    df["battery_charge_kw"] = (df["total_demand_kw"] - df["it_kw"] - df["building_kw"]
                               - df["cooling_kw"]).clip(lower=0)
    df["battery_net_out_kw"] = df["battery_out_kw"] - df["battery_charge_kw"]

    lp_path = results_dir / "battery_optimal_results.csv"
    if lp_path.exists():
        lp = pd.read_csv(lp_path).set_index("time")
        lp = lp.rename(columns={"battery_setpoint_kw": "lp_battery_setpoint_kw",
                                "battery_soc_opt": "lp_soc", "grid_power_opt": "lp_grid_kw",
                                "energy_cost_opt": "lp_energy_cost_eur"})
        df = df.join(lp, how="left")

    df.index = start + pd.to_timedelta(df.index, unit="s")
    df.index.name = "time"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    df.round(6).to_csv(out_csv)
    return df


def load_reference(csv: Path | str) -> pd.DataFrame:
    """Load an extracted reference CSV with a DatetimeIndex."""
    csv = Path(csv)
    if not csv.is_absolute():
        csv = DATA_DIR / csv
    return pd.read_csv(csv, parse_dates=["time"]).set_index("time")
