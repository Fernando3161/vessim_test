# Data

Small input traces used by the notebooks. Generated once; notebooks never fetch data online.

| File | Source | How it was produced |
|------|--------|---------------------|
| `e01_edge_s1_2_15min.csv` | Edge study `s1_2_battery_pv_large`, 2025-04-01, 1 day (edge commit `3e8533a`) | `scenario.py` run in `external/.venv-edge`, then `vessim_eval.edge_reference.extract_reference(..., step_s=900)` |
| `e01_edge_s1_2_pv_x3_15min.csv` | Same scenario with `pv_config.area_m2 = 15000` (3x) | Same procedure; config written to `external/configs-e01/s1_2_pv_x3.json` |

Column conventions (all power in kW):

- `*_kw`: value at the start of each 15-min step (what the edge study's grid accounting uses).
- `*_mean_kw`: time-weighted mean over the step (edge results are event-based, mostly 30 s).
- `grid_import_kw` positive = import; `battery_out_kw` and `lp_battery_setpoint_kw` positive = discharge.
- `soc`, `lp_soc`: state of charge at the start of the step (fraction).
- `lp_*`: offline-optimal battery schedule computed by the edge study (`battery_optimal_results.csv`).
