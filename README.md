# vessim_test

Lightweight experiments that evaluate [Vessim](https://github.com/dos-group/vessim)
([docs](https://vessim.readthedocs.io/en/latest/)) against our
[grid-serving edge data center study](https://gitlab.com/mosaik/application-scenarios/grid-serving-edge-data-centers)
([paper](https://ieeexplore.ieee.org/document/11670324)).

The repository answers four questions:

- Which Vessim features could we adopt in the edge data center study?
- What does Vessim cover that our study does not?
- Where is our study stronger than Vessim?
- What are Vessim's limitations — in particular workload management, thermal modelling and
  flexibility management?

See [docs/REPO_OBJECTIVES.md](docs/REPO_OBJECTIVES.md) for goals and hypotheses, and
[docs/EXPERIMENT_PLAN.md](docs/EXPERIMENT_PLAN.md) for the experiments.

## Experiments

| ID | Topic | Status |
|----|-------|--------|
| E00 | Setup and smoke test | done — [report](docs/reports/E00_setup_smoke.md) |
| E01 | Energy-side equivalence with the edge study | done — [report](docs/reports/E01_energy_equivalence.md) |
| E02 | Dispatch policies and storage models | done — [report](docs/reports/E02_dispatch_and_storage.md) |
| E03 | Dynamic workload | done — [report](docs/reports/E03_dynamic_workload.md) |
| E04 | Thermal and cooling | done — [report](docs/reports/E04_thermal_cooling.md) |
| E05 | Grid-serving flexibility | done — [report](docs/reports/E05_grid_flexibility.md) |
| E06 | Metrics, recording and viewer | planned |
| E07 | Mosaik coupling with an edge study simulator | planned |
| E08 | Multi-site federation | planned |
| E09 | Performance and scalability | planned |
| E10 | Live / software-in-the-loop (optional) | planned |

Each experiment runs on its own branch (`exp/eXX-<short-name>`), has one notebook in
`notebooks/`, and one report in `docs/reports/`.

## Quick start (Windows, PowerShell)

```powershell
# Create the local environment (skip if .venv already exists)
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m ipykernel install --user --name vessim_test --display-name "Python (vessim_test .venv)"

# Optional: clone the reference repositories for reading (git-ignored)
git clone --depth 1 https://github.com/dos-group/vessim.git external/vessim
git clone --depth 1 https://gitlab.com/mosaik/application-scenarios/grid-serving-edge-data-centers.git external/edge-dc-study

# Start Jupyter on the local environment
.venv\Scripts\python.exe -m jupyter lab
```

## Repository layout

```
docs/                 objectives, experiment plan, reports
notebooks/            one notebook per experiment
src/vessim_eval/      helper code used by the notebooks
data/                 small input traces (committed)
results/              generated outputs (ignored)
external/             cloned reference repositories (ignored)
```

## Conventions

Working rules for humans and coding agents — environment, branching, plotting style, notebook
structure — are in [AGENTS.md](AGENTS.md). In short: everything runs in `.venv`, experiments
are short (≤ 1 simulated day by default), plots are interactive Plotly figures with seaborn
palettes and plain-English axis labels, and nothing is committed without review.
