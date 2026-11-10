# Experiment Plan

Each experiment is small, fast, and answers one question. Each lives in its own notebook, on
its own Git branch, and produces one Markdown report. See `REPO_OBJECTIVES.md` for the
hypotheses (H1–H7) referenced below.

## Global constraints

| Constraint | Value |
|------------|-------|
| Simulated horizon | 1 day by default; at most 7 days; never a full year |
| Wall time per notebook | target < 1 min, hard limit ~5 min |
| Step size | 60 s to 15 min (15 min matches the edge study's physical step) |
| Data | Small CSV traces in `data/` (< 1 MB each) or generated synthetically in code |
| Randomness | Fixed seeds; record the seed in the notebook and report |
| Environment | `.venv` only (see `AGENTS.md`) |

## Common reference scenario

To make results comparable across experiments, every experiment starts from (or deviates
explicitly from) one **reference edge site**, a scaled-down version of edge study scenario
S1.2 (battery + PV):

| Component | Reference value | Source |
|-----------|-----------------|--------|
| IT load | 4 440 CPU cores, 7.3 W peak per core, idle 21.7 % of peak, ~80 % mean utilisation, daily profile | edge study `hardware.py`, job generator |
| Facility base load | constant, from edge study `building_config` | edge study config |
| Cooling | PUE-style overhead ≈ 0.285 × IT (edge study controller assumption) | edge study `controller.py` |
| PV | 1 000 m² array, one spring day (2025-04-01) | edge study S1.2 |
| Battery | 500 kWh | edge study S1.1/S1.2 |
| Grid | unlimited import/export unless stated | — |
| Price signal | day-ahead-like hourly price trace | edge study `energy_market.py` or synthetic |
| Carbon signal | hourly grid carbon intensity trace | synthetic or public dataset |

Traces derived from the edge study are generated once (from its code or a single 1-day run in
a separate environment) and stored as small CSVs in `data/`.

## Experiment overview

| ID | Branch | Question | Hypotheses | Priority |
|----|--------|----------|------------|----------|
| E00 | `exp/e00-setup-smoke` | Does the toolchain work, and does Vessim conserve energy on the reference site? | H4 | 1 |
| E01 | `exp/e01-energy-equivalence` | Given identical inputs, do Vessim and the edge study produce the same grid exchange and battery state? | H4 | 1 |
| E02 | `exp/e02-dispatch-and-storage` | How expressive are Vessim's dispatch policies and battery models? | H3, H4 | 1 |
| E03 | `exp/e03-dynamic-workload` | How much effort does it take to add a job-based, shiftable workload to Vessim, and what does it give? | H1 | 1 |
| E04 | `exp/e04-thermal-cooling` | Can cooling and heat recovery be represented in Vessim, and with what fidelity loss? | H2 | 1 |
| E05 | `exp/e05-grid-flexibility` | Can Vessim express grid-serving flexibility (limits, demand response, flexibility bands)? | H3 | 1 |
| E06 | `exp/e06-metrics-recording` | Do Vessim's metrics, recorder and viewer cover our cost/CO₂ evaluation needs? | H5 | 2 |
| E07 | `exp/e07-mosaik-coupling` | Can an edge study simulator be plugged into Vessim through mosaik? | H6 | 2 |
| E08 | `exp/e08-multi-site` | Can Vessim model a federation of edge sites with spatial load shifting? | H1, H3 | 2 |
| E09 | `exp/e09-performance` | How do wall time and memory scale with step size and number of sites? | H7 | 3 |
| E10 | `exp/e10-live-sil` (optional) | Is Vessim's live / software-in-the-loop mode useful for testing a real controller? | H5 | 3 |

Priority 1 experiments map directly to the suspected limitations and should run first.

---

## E00 — Setup and smoke test

- **Question:** Does Vessim install and run in `.venv`, and does the reference site respect the
  power balance at every step?
- **Setup:** Reference site, 1 day, 5-min steps, default dispatch policy.
- **Measures:** Residual of the power balance per step; energy totals (consumed, produced,
  imported, exported, stored); wall time.
- **Plots:** Stacked active power by component over time; battery state of charge; energy
  balance bar chart.
- **Done when:** Balance residual ≈ 0, totals close, helper modules for the reference site and
  plotting style exist in `src/vessim_eval/`.

## E01 — Energy-side equivalence with the edge study

- **Question:** With the same load, PV and battery inputs, do both tools agree?
- **Setup:** Export the edge study's 1-day S1.2 load and PV time series (15-min steps). Feed
  them into Vessim as `Trace` signals. Use `SimpleBattery` with matching capacity and limits.
  Compare against the edge study's recorded grid power and SOC.
- **Measures:** Grid import/export energy, peak import, battery SOC trajectory, absolute and
  relative differences; explanation of each divergence (efficiency, C-rate, dispatch order,
  sign convention, time alignment).
- **Plots:** Overlaid active grid power (Vessim vs. edge study); SOC overlay; difference
  time series; parity scatter.
- **Expected:** Close agreement once conventions are aligned; differences point to model
  assumptions worth documenting.

## E02 — Dispatch policies and storage models

- **Question:** What control logic can Vessim express natively, and how do its battery models
  differ?
- **Setup:** Reference site, 1 day. Variants:
  1. `DefaultDispatchPolicy` (greedy, priority order);
  2. fixed `charge_power`;
  3. a custom price-aware `DispatchPolicy` (charge at low price, discharge at high);
  4. `SimpleBattery` vs. `ClcBattery`;
  5. step-size sensitivity (1 min, 5 min, 15 min).
- **Measures:** Grid cost, peak import, battery cycles, SOC limits respected, lines of code
  needed for the custom policy, effect of step size on results.
- **Plots:** Grid active power per policy; SOC per battery model; cost vs. peak import
  scatter; step-size sensitivity bars.
- **Probes H3:** Policies only see the current step (`p_delta`, current signals). Any
  look-ahead must be built by the user.

## E03 — Dynamic workload

- **Question:** Vessim treats demand as a power signal. How hard is it to add jobs, queues and
  temporal shifting, and what do we lose compared to the edge study's MILP scheduler?
- **Setup:** Build a minimal job model in `src/vessim_eval/workload.py`: job arrivals
  (sampled from the edge study's job generator statistics, fixed seed), CPU demand, duration,
  deadline/flexibility flag; a custom `Actor` that turns running jobs into active power; a
  `Controller` that defers flexible jobs to low-price or high-PV periods (greedy rule).
  Compare:
  1. static power trace (Vessim native);
  2. job-based, run-on-arrival;
  3. job-based, greedy shifting.
- **Measures:** Energy shifted, cost and CO₂ change, deadline violations, queue length, lines
  of custom code, what had to be worked around (e.g. one-step delay between controller
  action and observed demand).
- **Plots:** Active power of IT load per variant; queue length; cumulative cost; job
  start-time shift histogram.
- **Expected:** Confirms H1; quantifies effort to retrofit workload.

## E04 — Thermal and cooling

- **Question:** Can cooling, ambient-temperature dependence and waste heat be represented?
- **Setup:** Variants:
  1. constant PUE as a second `Consumer`;
  2. custom `Actor` whose cooling power depends on IT load and an outdoor temperature
     `Trace` (COP curve);
  3. custom `Actor` with a lumped thermal state (single RC node for room temperature) to test
     whether stateful thermal models fit Vessim's actor interface;
  4. (stretch) waste heat as a thermal output — check that Vessim has no place to put it.
- **Measures:** Cooling electrical energy, sensitivity to outdoor temperature, room
  temperature trajectory, what the edge study models that cannot be expressed (heat pumps,
  buffers, district heating, multi-component cooling chain).
- **Plots:** IT vs. cooling active power; outdoor temperature vs. cooling power; room
  temperature trajectory.
- **Expected:** Confirms H2; electrical-only approximations are possible, heat flows are not
  first-class.

## E05 — Grid-serving flexibility

- **Question:** Can Vessim express what the edge study calls grid-serving flexibility?
- **Setup:** Reference site with battery and a flexible load.
  1. Import limit and ramp limits on `GridConnection`;
  2. a demand-response event (temporary import cap announced one step ahead), following
     Vessim's `demand_response.py` example;
  3. compute a flexibility band (minimum / reference / maximum grid power per step) from
     Vessim's state and compare with the edge study's `flexibility_results.csv` concept;
  4. islanding (grid disconnected for a window).
- **Measures:** Whether constraints are respected; unmet demand handling (Vessim raises
  errors instead of curtailing); band width over the day; effort to compute it.
- **Plots:** Grid active power with limit envelope; flexibility band (shaded) vs. actual;
  battery SOC during the event.
- **Expected:** Confirms or nuances H3: hard grid constraints exist, but flexibility
  quantification and forecasting do not.

## E06 — Metrics, recording and viewer

- **Question:** Do Vessim's evaluation tools cover our KPIs (electricity cost, revenue, CO₂,
  peak, self-consumption)?
- **Setup:** Reference site, 1 day, with `ElectricityImportCost`, `ElectricityExportRevenue`,
  `GridImportEmissions` and one custom `Metric` (self-consumption ratio). Export the
  experiment record and open it in the Vessim viewer.
- **Measures:** Metric values vs. manual calculation; ease of adding custom metrics;
  experiment record content.
- **Plots:** KPI comparison bar chart; cumulative cost and emissions.

## E07 — Mosaik coupling with an edge study simulator

- **Question:** Can our simulators run inside a Vessim environment?
- **Setup:** Use Vessim's mosaik backend. Couple a lightweight edge study simulator (first
  candidate: `hardware.py` power model, or a stub that replays its output) as the site
  demand. Optionally feed Vessim's battery SOC back.
- **Measures:** Whether coupling works; data-flow direction and timing; effort; dependency
  conflicts between the two projects (mosaik versions, Python versions).
- **Plots:** Coupled active power vs. stand-alone run.
- **Notes:** Edge study dependencies stay outside `.venv` if they conflict; prefer a stub
  simulator that reproduces the interface.

## E08 — Multi-site federation

- **Question:** Can several edge sites with spatial load shifting be modelled?
- **Setup:** Three microgrids (different PV profiles), a `Controller` that moves flexible
  load between sites based on local surplus. Compare with edge study S3.1 concepts.
- **Measures:** Load moved, grid import per site, what is missing (network latency, job
  routing, shared state).
- **Plots:** Active power per site; load transfer between sites.

## E09 — Performance and scalability

- **Question:** How fast is Vessim for our typical scenario sizes?
- **Setup:** Reference site at step sizes 60 s – 15 min; 1, 3, 10, 30 microgrids; 1 day only.
  Default backend vs. mosaik backend.
- **Measures:** Wall time, steps per second, peak memory.
- **Plots:** Wall time vs. number of steps (log-log); wall time vs. number of sites.

## E10 — Live / software-in-the-loop (optional)

- **Question:** Is the REST API / live mode useful to test a real scheduler against a
  simulated energy system?
- **Setup:** `vs.Environment.live()` with the `Api` controller, accelerated clock, a short
  script polling and setting values. Only if time allows; requires the `live` extra.

---

## Report template

Each experiment report goes to `docs/reports/EXX_<short_name>.md`:

```markdown
# EXX — <Title>

- Branch: exp/eXX-...
- Notebook: notebooks/EXX_<short_name>.ipynb
- Date: YYYY-MM-DD
- Vessim version / commit:
- Wall time:

## Question
## Setup (what was simulated, parameters, seed)
## Results (key numbers in a table, 2–4 figures exported from the notebook)
## Interpretation
## Hypotheses (H#: confirmed / rejected / partial — evidence)
## Implications for the edge data center study (adopt / ignore / positioning argument)
## Limitations of this experiment
```

## Synthesis

After the priority-1 experiments, write `docs/reports/SYNTHESIS.md` with the capability matrix
and adoption recommendations, then refine it after each further experiment.
