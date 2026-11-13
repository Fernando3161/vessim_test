# Data

Small input traces used by the notebooks. Generated once; notebooks never fetch data online.

| File | Source | How it was produced |
|------|--------|---------------------|
| `e01_edge_s1_2_15min.csv` | Edge study `s1_2_battery_pv_large`, 2025-04-01, 1 day (edge commit `3e8533a`) | `scenario.py` run in `external/.venv-edge`, then `vessim_eval.edge_reference.extract_reference(..., step_s=900)` |
| `e01_edge_s1_2_pv_x3_15min.csv` | Same scenario with `pv_config.area_m2 = 15000` (3x) | Same procedure; config written to `external/configs-e01/s1_2_pv_x3.json` |
| `e03_edge_jobs_60_OL_20.csv` | Edge study job trace `config/aggregated_jobs_60_OL_20.json` (used by S1.2; 7 279 jobs, one day) | `vessim_eval.workload.extract_jobs(...)`: keeps `job_id`, `arrival_s`, `cpu` (3 decimals), `runtime_s`, `delay_tolerant`, `max_delay_s`; drops RAM and spatial-flexibility fields (all migration fields are 0 in this trace) |
| `e04_edge_s1_2_cooling_15min.csv` | Edge study S1.2 run of E01 (`results/e01_s1_2`, heat mode `none`) | `vessim_eval.thermal.extract_thermal_reference(..., step_s=900)`: IT power, outdoor temperature and cooling-chain components (chiller, CRAH fan, two pumps, heat removed) at the start of each 15-min step, plus the step-mean IT power |
| `e04_edge_s2_2_heat_15min.csv` | Edge study `s2_2_waste_heat_warm`, 2025-04-04, 1 day (edge commit `3e8533a`, run once in `external/.venv-edge`, 161 s) | Same function: cooling chain plus heat pump (power, COP, source temperatures), hot-water tank temperatures and protection flag, district-heating demand, heat accepted by the network and heat-exchanger output |
| `e05_edge_s1_2_flexibility_15min.csv` | Edge study S1.2 run of E01 (`results/e01_s1_2/flexibility_results.csv`, 1.4 MB, 2 555 scheduler calls at horizon step 0) | `vessim_eval.flexibility.extract_edge_band(..., start="2025-04-01")`: keeps horizon step 0, converts each band to offsets from `p_grid_ref` (job, MPC battery, offline-optimal battery; down and up, kW), adds the horizon-step-1 reference (`p_ref_h1_kw`), averages per 15 min and counts the calls (`n_calls`) |

Column conventions (all power in kW):

- `*_kw`: value at the start of each 15-min step (what the edge study's grid accounting uses).
- `*_mean_kw`: time-weighted mean over the step (edge results are event-based, mostly 30 s).
- `grid_import_kw` positive = import; `battery_out_kw` and `lp_battery_setpoint_kw` positive = discharge.
- `soc`, `lp_soc`: state of charge at the start of the step (fraction).
- `lp_*`: offline-optimal battery schedule computed by the edge study (`battery_optimal_results.csv`).
