# Repository Objectives

## Purpose

This repository evaluates [Vessim](https://github.com/dos-group/vessim) — a Python co-simulation
testbed for data-center energy systems — against our **grid-serving edge data center study**
([GitLab](https://gitlab.com/mosaik/application-scenarios/grid-serving-edge-data-centers),
[paper](https://ieeexplore.ieee.org/document/11670324)).

The goal is not to reproduce either tool in full. It is to answer, with small and fast
experiments, four questions:

1. **What can we adopt?** Which Vessim features, abstractions or design patterns could be
   implemented in, or reused by, the edge data center study?
2. **What does Vessim cover that we do not?** Capabilities present in Vessim and absent in the
   edge study.
3. **Where is our study stronger?** Capabilities the edge study models that Vessim does not,
   or models only superficially.
4. **What are Vessim's limitations?** Hard limits (missing model classes), soft limits
   (possible but awkward through extension points), and numerical / performance limits.

## Working hypotheses (to be confirmed or rejected)

From reading the Vessim source (commit `3feef2d`, 2026-10-20) and documentation:

| ID | Hypothesis | Tested in |
|----|------------|-----------|
| H1 | Vessim has **no workload model**: demand is an exogenous power signal; there are no jobs, queues, deadlines or CPU/RAM resources. Workload behaviour must be written by the user as a custom `Signal`/`Actor` + `Controller`. | E03 |
| H2 | Vessim has **no thermal model**: no cooling chain, no IT heat, no ambient-temperature dependence, no heat recovery. Cooling can only be approximated as an additional electrical consumer. | E04 |
| H3 | Vessim's **flexibility management is weak**: the dispatch policy is a greedy, single-step, priority-ordered balance; there is no look-ahead, no flexibility band (min/ref/max grid power) and no forecast interface. | E02, E05 |
| H4 | Vessim's **energy-side abstractions are cleaner** than ours: strict power-balance invariants, pluggable dispatch policies, grid import/export limits and ramps, islanding, and an electrochemical battery model (C-L-C). | E00, E02 |
| H5 | Vessim's **experiment tooling is stronger**: built-in recorder, metric framework (cost, emissions), experiment record format, viewer, live / software-in-the-loop mode. | E06, E08 |
| H6 | Vessim can **host our models** through its optional mosaik backend, so a hybrid (Vessim energy layer + our workload/thermal simulators) is feasible. | E07 |
| H7 | Vessim is **fast** for short horizons and many microgrids, because its default engine is pure Python without mosaik overhead. | E09 |

## Scope

**In scope**

- Short, reproducible experiments (one to a few simulated days, seconds to a few minutes wall
  time).
- Side-by-side comparisons on equivalent, simplified scenarios (same load, same PV, same
  battery) where both tools can be configured identically.
- Probing Vessim's extension points (custom `Signal`, `Actor`, `Dispatchable`,
  `DispatchPolicy`, `Controller`, `Metric`, mosaik coupling) to judge how much effort it takes
  to add what is missing.
- A written report per experiment and a final synthesis.

**Out of scope**

- Year-long or multi-week simulations, parameter sweeps over hundreds of runs, or anything
  requiring an HPC setup.
- Re-implementing the full edge study inside Vessim.
- Modifying Vessim upstream (findings may lead to upstream issues, but not in this repo).
- Calibrating either tool against real measurements.

## Deliverables

| Deliverable | Location |
|-------------|----------|
| Experiment notebooks (one per experiment) | `notebooks/` |
| Reusable helper code (scenario builders, adapters, plotting) | `src/vessim_eval/` |
| Experiment plan | `docs/EXPERIMENT_PLAN.md` |
| One Markdown report per experiment | `docs/reports/` |
| Final synthesis: capability matrix + adoption recommendations | `docs/reports/SYNTHESIS.md` (written last) |

## Success criteria

The evaluation is complete when:

- every hypothesis H1–H7 is marked **confirmed**, **rejected**, or **partially confirmed** with
  evidence from at least one experiment;
- a capability matrix (Vessim vs. edge study) exists with each cell backed by an experiment or
  a source-code reference;
- there is a ranked list of concrete Vessim features to adopt in the edge study, each with an
  effort estimate (low / medium / high);
- there is a list of edge study capabilities that Vessim lacks, usable as positioning
  arguments for our paper.

## Reference snapshot

| Tool | Revision used | Notes |
|------|---------------|-------|
| Vessim | `3feef2d` (GitHub `main`, 2026-10-20) and the PyPI release installed in `.venv` | Python 3.9–3.15, core deps: numpy, pandas, loguru; optional `mosaik`, `live` extras |
| Edge DC study | `3e8533a` (GitLab, 2026-08-20) | mosaik-based, Python 3.12, MILP scheduler (HiGHS), Open-Meteo weather |

If either tool is updated during the evaluation, record the new revision in the affected
experiment report.
