# AGENTS.md

Instructions for coding agents (Claude Code and others) working in this repository.
Read `docs/REPO_OBJECTIVES.md` and `docs/EXPERIMENT_PLAN.md` before starting any task.

## What this repository is

A set of small, fast experiments that evaluate the Vessim data-center energy simulator against
our grid-serving edge data center study. The output is evidence (notebooks, plots, reports),
not a product.

## Hard rules

1. **Use the local `.venv` only.** Never install into the base Anaconda or system Python.
   - Python: `.venv/Scripts/python.exe` (Windows) — Python 3.13.
   - Install: `.venv/Scripts/python.exe -m pip install <pkg>`, then add the package to
     `requirements.txt`.
   - If `.venv` is missing, recreate it: `python -m venv .venv` (or `uv venv .venv`), then
     `pip install -r requirements.txt` and `pip install -e .` once a `pyproject.toml` exists.
   - Notebooks must run on the `.venv` kernel.
2. **One branch per experiment.** Branch name `exp/eXX-<short-name>` as listed in
   `docs/EXPERIMENT_PLAN.md`. Create it from `main` before touching experiment code.
   Documentation-only changes that are not part of an experiment go on a `docs/<topic>` branch.
3. **Never commit or push without explicit user approval.** Prepare changes, summarise them,
   and wait for review. Never merge branches, rebase shared branches or force-push.
4. **Keep experiments lightweight.** Default horizon 1 simulated day, at most 7. Target < 1 min
   wall time per notebook. No year-long runs, no large sweeps.
5. **Do not bloat the repository.**
   - External repositories (Vessim, edge study) are cloned into `external/`, which is
     git-ignored. Never copy their source into `src/`.
   - Simulation outputs go to `results/` (git-ignored).
   - Only small input traces (< 1 MB, CSV) are committed, in `data/`.
   - No PDFs, no large binaries. Papers stay out of Git (`external/papers/` is ignored).
   - Clear notebook outputs only if the user asks; by default keep outputs so plots render on
     review.
6. **Helper code lives in `src/vessim_eval/`, not in notebooks.** Notebooks call functions;
   they do not define long helpers. If a cell grows beyond ~30 lines of logic, move it to
   `src/`.

## Repository layout

```
AGENTS.md                 this file
CLAUDE.md                 pointer to AGENTS.md for Claude Code
README.md                 overview and quick start
requirements.txt          dependencies for .venv
docs/
  REPO_OBJECTIVES.md      goals, hypotheses H1–H7, scope
  EXPERIMENT_PLAN.md      experiments E00–E10, constraints, report template
  reports/                one Markdown report per experiment + SYNTHESIS.md
notebooks/                one notebook per experiment: EXX_<short_name>.ipynb
src/vessim_eval/          helper package (scenarios, adapters, workload, thermal, plotting)
data/                     small committed input traces
results/                  generated outputs (ignored)
external/                 cloned vessim + edge study repos (ignored)
```

## Notebook structure

Every experiment notebook has exactly these sections, as Markdown headings:

1. **Experiment** — title, question, hypotheses tested, branch name, date.
2. **Setup** — imports, parameters in one cell (horizon, step size, seed, component sizes),
   scenario construction through `src/vessim_eval` helpers.
3. **Run** — simulation runs, with wall time printed.
4. **Results** — tables of key numbers and interactive plots.
5. **Evaluation** — short bullet interpretation, hypothesis verdicts, notes for the report.

Notebooks must run top to bottom with "Restart & Run All" and no manual steps.

## Plotting conventions

All plots are **interactive** (Plotly) and styled for scientific presentation. Implement the
style once in `src/vessim_eval/plotting.py` and reuse it.

- **Palette:** seaborn palettes converted to hex (`sns.color_palette("colorblind").as_hex()`
  for categories; `"rocket"`/`"mako"` for sequential data). Keep a fixed colour per component
  across all notebooks (for example grid, PV, battery, IT load, cooling).
- **Distinguishable series:** combine colour with line dash or marker so lines remain distinct
  in greyscale; use clear bar outlines and spacing for columns.
- **Labels in plain English with units in parentheses:** "Active power (kW)", "Battery state
  of charge (%)", "Grid import energy (kWh)", "Outdoor temperature (°C)". Never symbols like
  `P/MW` or variable names like `p_grid`.
- **Units:** SI, scaled to readable magnitudes (kW/kWh for an edge site). Use scientific
  notation on axes only when values span orders of magnitude
  (`exponentformat="power"`).
- **Layout:** white template, light grid, legend outside or above the plot area, time axis in
  local wall-clock time, shared x-axes for stacked subplots, titles that state what is shown.
- **Sign convention in plots:** state it in the axis title or caption (for example "Grid
  active power (kW, positive = import)"). Vessim's internal convention is W with generation and
  export positive; convert explicitly in helper code.
- Figures needed for reports are exported to `results/<experiment>/` (HTML, and PNG via
  `kaleido` when needed) and copied to `docs/reports/figures/` only if they are small.

## Code conventions

- Python ≥ 3.10 syntax, type hints on public functions, NumPy-style docstrings kept short.
- Units in variable names where ambiguous (`power_w`, `energy_kwh`, `step_s`).
- Fixed random seeds, passed as parameters.
- No network access during notebook runs; fetch or generate data once and store it in
  `data/`.
- Keep functions small and generic enough to be reused across experiments.

## Working with the external repositories

- Vessim: `external/vessim` (read the source to understand behaviour; the installed package
  in `.venv` is what experiments run against — record its version in each report).
- Edge study: `external/edge-dc-study`. It pins its own dependencies (Python 3.12, mosaik
  plugins, numpy). Do **not** install them into `.venv` if they conflict. If an edge study run
  is needed to produce reference traces, use a separate environment (for example
  `external/.venv-edge`, ignored) and store only the resulting small CSVs in `data/`.
- When reasoning about the edge study, cite file paths (for example
  `external/edge-dc-study/simulators/controller.py`) in reports.

## Reports

After each experiment, write `docs/reports/EXX_<short_name>.md` following the template in
`docs/EXPERIMENT_PLAN.md`. State hypothesis verdicts explicitly and give implications for the
edge data center study. Report failures and dead ends honestly — they are results.

## Workflow per experiment

1. Confirm the experiment ID and scope with the user if unclear.
2. `git checkout main && git checkout -b exp/eXX-<short-name>`.
3. Add or extend helpers in `src/vessim_eval/`; add a minimal test if logic is non-trivial.
4. Build the notebook following the structure above; run it end to end in `.venv`.
5. Write the report.
6. Summarise changed files and findings to the user. **Stop and wait for review before
   committing.**
