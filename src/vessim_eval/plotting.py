"""Shared interactive plotting style (Plotly + seaborn palettes).

Conventions (see AGENTS.md): plain-English axis labels with units in parentheses,
fixed colour per component, colour combined with dash style so series stay distinct.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
import seaborn as sns
from plotly.subplots import make_subplots

PALETTE = sns.color_palette("colorblind").as_hex()

# Fixed colour per component across all notebooks.
COMPONENT_COLORS = {
    "load": PALETTE[0],      # blue
    "pv": PALETTE[8],        # yellow
    "grid": PALETTE[2],      # green
    "battery": PALETTE[4],   # purple
    "cooling": PALETTE[9],   # light blue
    "it": PALETTE[0],
    "building": PALETTE[5],  # brown
    "price": PALETTE[7],
    "residual": PALETTE[7],
}

# Fixed colour + dash per model / variant across all notebooks.
VARIANT_STYLES = {
    "Edge study": {"color": "#222222", "dash": "solid", "width": 3},
    "Edge study, optimal schedule": {"color": "#7f7f7f", "dash": "solid", "width": 3},
    "Vessim default": {"color": PALETTE[0], "dash": "dash", "width": 2},
    "Vessim default + losses": {"color": PALETTE[1], "dash": "longdash", "width": 2},
    "Vessim + edge rule": {"color": PALETTE[2], "dash": "dot", "width": 2.5},
    "Vessim + edge rule + losses": {"color": PALETTE[2], "dash": "dot", "width": 2.5},
    "Vessim, optimal schedule replay": {"color": PALETTE[3], "dash": "dot", "width": 2.5},
    "Sampled at step start": {"color": PALETTE[0], "dash": "solid", "width": 2},
    "Time-weighted step mean": {"color": PALETTE[1], "dash": "dash", "width": 2},
    # E02: dispatch policies
    "Greedy (Vessim default)": {"color": PALETTE[0], "dash": "dash", "width": 2},
    "Fixed discharge power": {"color": PALETTE[7], "dash": "dashdot", "width": 2},
    "Time-of-use windows": {"color": PALETTE[6], "dash": "longdash", "width": 2.5},
    "Price thresholds": {"color": PALETTE[4], "dash": "solid", "width": 2},
    "Peak shaving": {"color": PALETTE[2], "dash": "dot", "width": 2.5},
    "Look-ahead, minimum cost": {"color": PALETTE[3], "dash": "solid", "width": 2.5},
    "Look-ahead, minimum peak": {"color": "#222222", "dash": "dash", "width": 2},
    # E02: battery models
    "SimpleBattery (Vessim)": {"color": PALETTE[0], "dash": "dash", "width": 2},
    "EfficiencyBattery (edge parameters)": {"color": PALETTE[1], "dash": "solid", "width": 2.5},
    "ClcBattery (Vessim)": {"color": PALETTE[2], "dash": "dot", "width": 2.5},
    "Requested schedule": {"color": "#222222", "dash": "solid", "width": 1.5},
    # E03: workload variants
    "Static IT trace (edge study output)": {"color": "#222222", "dash": "solid", "width": 3},
    "Jobs, start on arrival": {"color": PALETTE[0], "dash": "dash", "width": 2},
    "Jobs, cheapest price": {"color": PALETTE[4], "dash": "solid", "width": 2},
    "Jobs, lowest peak": {"color": PALETTE[2], "dash": "dot", "width": 2.5},
    "Jobs, hold on high import": {"color": PALETTE[3], "dash": "dashdot", "width": 2},
    # E04: cooling models and heat recovery
    "Constant overhead (0.285 × IT)": {"color": PALETTE[0], "dash": "dash", "width": 2},
    "Linear fit to edge run": {"color": PALETTE[4], "dash": "dashdot", "width": 2},
    "Ported edge cooling chain": {"color": PALETTE[2], "dash": "dot", "width": 3},
    "IT power": {"color": PALETTE[7], "dash": "solid", "width": 2},
    "No event": {"color": "#222222", "dash": "solid", "width": 2},
    "Vessim heat-recovery actor": {"color": PALETTE[3], "dash": "dash", "width": 2.5},
    "District-heating demand": {"color": PALETTE[7], "dash": "dot", "width": 2},
    # E02: step sizes
    "15 min": {"color": PALETTE[0], "dash": "solid", "width": 2},
    "5 min": {"color": PALETTE[1], "dash": "dash", "width": 2},
    "1 min": {"color": PALETTE[2], "dash": "dot", "width": 2},
}
_FALLBACK_DASHES = ["solid", "dash", "dot", "dashdot", "longdash", "longdashdot"]

FONT = dict(family="Arial, Helvetica, sans-serif", size=13, color="#222222")


def _template() -> go.layout.Template:
    t = go.layout.Template(pio.templates["simple_white"])
    t.layout.font = FONT
    t.layout.colorway = PALETTE
    axis = dict(showgrid=True, gridcolor="#e5e5e5", ticks="outside", mirror=True,
                showline=True, linecolor="#444444", zeroline=False, automargin=True)
    t.layout.xaxis = axis
    t.layout.yaxis = {**axis, "title": {"standoff": 12}}
    # Title pinned to the top of the figure (container coordinates); legend sits just above
    # the plot area (paper coordinates). The top margin reserves room for both, including a
    # legend that wraps onto two rows, so they never overlap.
    t.layout.title = dict(x=0, xanchor="left", xref="paper", y=1, yanchor="top",
                          yref="container", pad=dict(t=16), font=dict(size=16))
    t.layout.legend = dict(orientation="h", yanchor="bottom", y=1.02, yref="paper",
                           xanchor="left", x=0, bgcolor="rgba(0,0,0,0)")
    t.layout.hovermode = "x unified"
    t.layout.margin = dict(l=70, r=30, t=130, b=60)
    return t


pio.templates["vessim_eval"] = _template()
pio.templates.default = "vessim_eval"


def variant_style(name: str, index: int = 0) -> dict:
    """Line style for a named variant, falling back to palette + dash cycling."""
    if name in VARIANT_STYLES:
        return VARIANT_STYLES[name]
    return {"color": PALETTE[index % len(PALETTE)],
            "dash": _FALLBACK_DASHES[index % len(_FALLBACK_DASHES)], "width": 2}


def line_comparison(
    series: Mapping[str, pd.Series],
    title: str,
    y_title: str,
    x_title: str = "Time of day",
    height: int = 420,
    shape: str = "hv",
) -> go.Figure:
    """Overlay several time series (one per variant) on one axis."""
    fig = go.Figure()
    for i, (name, s) in enumerate(series.items()):
        st = variant_style(name, i)
        fig.add_trace(go.Scatter(
            x=s.index, y=s.values, name=name, mode="lines", line_shape=shape,
            line=dict(color=st["color"], dash=st["dash"], width=st["width"]),
        ))
    fig.update_layout(title=title, height=height, xaxis_title=x_title, yaxis_title=y_title)
    return fig


def stacked_panels(
    panels: list[tuple[str, Mapping[str, pd.Series]]],
    title: str,
    x_title: str = "Time of day",
    row_height: int = 260,
    shape: str = "hv",
) -> go.Figure:
    """Vertically stacked panels with a shared time axis.

    ``panels`` is a list of (y-axis title, {variant name: series}).
    Variant styles are consistent across panels and the legend shows each once.
    """
    fig = make_subplots(rows=len(panels), cols=1, shared_xaxes=True, vertical_spacing=0.06)
    seen: set[str] = set()
    names: list[str] = []
    for _, group in panels:
        for n in group:
            if n not in names:
                names.append(n)
    for row, (y_title, group) in enumerate(panels, start=1):
        for name, s in group.items():
            st = variant_style(name, names.index(name))
            fig.add_trace(go.Scatter(
                x=s.index, y=s.values, name=name, legendgroup=name,
                showlegend=name not in seen, mode="lines", line_shape=shape,
                line=dict(color=st["color"], dash=st["dash"], width=st["width"]),
            ), row=row, col=1)
            seen.add(name)
        fig.update_yaxes(title_text=_wrap(y_title), row=row, col=1)
    fig.update_xaxes(title_text=x_title, row=len(panels), col=1)
    _frame_all_axes(fig)
    fig.update_layout(title=title, height=row_height * len(panels) + 140)
    return fig


def _wrap(label: str) -> str:
    """Put the unit part of a long axis label on a second line."""
    return label.replace(" (", "<br>(", 1) if len(label) > 28 else label


def _frame_all_axes(fig: go.Figure) -> None:
    """Template axis styling only reaches the first axes; apply it to subplots too."""
    style = dict(showgrid=True, gridcolor="#e5e5e5", ticks="outside", mirror=True,
                 showline=True, linecolor="#444444", zeroline=False, automargin=True)
    fig.update_xaxes(**style)
    fig.update_yaxes(**style, title_standoff=12)


def grouped_bars(
    df: pd.DataFrame,
    title: str,
    y_title: str,
    x_title: str = "",
    height: int = 420,
    log_y: bool = False,
    horizontal: bool = False,
) -> go.Figure:
    """Grouped bar chart: index = categories, columns = variants.

    ``horizontal=True`` puts categories on the y-axis (better for long labels);
    ``y_title`` then labels the value axis (x).
    """
    fig = go.Figure()
    cats = list(df.index)
    for i, col in enumerate(df.columns):
        st = variant_style(col, i)
        vals = df[col].values
        fig.add_trace(go.Bar(
            x=vals if horizontal else cats, y=cats if horizontal else vals,
            orientation="h" if horizontal else "v", name=col,
            marker=dict(color=st["color"], line=dict(color="#222222", width=1)),
            text=[f"{v:,.1f}" for v in vals], textposition="outside",
        ))
    value_axis = dict(type="log", exponentformat="power") if log_y else {}
    if horizontal:
        fig.update_layout(xaxis_title=y_title, yaxis_title=x_title)
        fig.update_xaxes(**value_axis)
        fig.update_yaxes(autorange="reversed")
    else:
        fig.update_layout(xaxis_title=x_title, yaxis_title=y_title)
        fig.update_yaxes(**value_axis)
    fig.update_layout(title=title, height=height, barmode="group", bargap=0.25,
                      bargroupgap=0.05, hovermode="closest", showlegend=len(df.columns) > 1)
    return fig


def parity_plot(
    reference: pd.Series,
    others: Mapping[str, pd.Series],
    title: str,
    axis_label: str,
    reference_name: str = "Edge study",
) -> go.Figure:
    """Scatter of each variant against the reference, with the identity line."""
    fig = go.Figure()
    lo = min([reference.min()] + [s.min() for s in others.values()])
    hi = max([reference.max()] + [s.max() for s in others.values()])
    fig.add_trace(go.Scatter(x=[lo, hi], y=[lo, hi], mode="lines", name="Perfect agreement",
                             line=dict(color="#888888", dash="dot", width=1)))
    symbols = ["circle", "square", "diamond", "triangle-up", "x"]
    for i, (name, s) in enumerate(others.items()):
        st = variant_style(name, i)
        fig.add_trace(go.Scatter(
            x=reference.values, y=s.reindex(reference.index).values, mode="markers", name=name,
            marker=dict(color=st["color"], symbol=symbols[i % len(symbols)], size=7,
                        line=dict(color="#222222", width=0.5), opacity=0.8),
        ))
    pad = 0.03 * (hi - lo)
    fig.update_layout(title=title, width=680, height=700, hovermode="closest",
                      xaxis_title=f"{axis_label}, {reference_name}",
                      yaxis_title=f"{axis_label}, Vessim")
    fig.update_xaxes(range=[lo - pad, hi + pad], constrain="domain")
    fig.update_yaxes(range=[lo - pad, hi + pad], scaleanchor="x", scaleratio=1,
                     constrain="domain")
    return fig


def power_balance(df: pd.DataFrame, title: str, height: int = 460) -> go.Figure:
    """Stacked active power of a standardized run (kW).

    Positive stack = supply (PV, battery discharge, grid import); negative stack =
    absorption (battery charge, grid export). The site demand is overlaid as a line, so
    a closed balance shows the positive stack meeting the demand line exactly.
    """
    parts = [
        ("Photovoltaic generation", df["pv_kw"], "pv", 1),
        ("Battery discharge", df["battery_kw"].clip(lower=0), "battery", 1),
        ("Grid import", df["grid_kw"].clip(lower=0), "grid", 1),
        ("Battery charge", df["battery_kw"].clip(upper=0), "battery", -1),
        ("Grid export", df["grid_kw"].clip(upper=0), "grid", -1),
    ]
    patterns = {"Battery charge": "/", "Grid export": "/"}
    fig = go.Figure()
    for name, s, key, sign in parts:
        if not s.abs().gt(1e-9).any():
            continue
        fig.add_trace(go.Scatter(
            x=s.index, y=s.values, name=name, mode="lines", line_shape="hv",
            stackgroup="supply" if sign > 0 else "absorb",
            line=dict(width=0.8, color=COMPONENT_COLORS[key]),
            fillpattern=dict(shape=patterns.get(name, "")),
        ))
    fig.add_trace(go.Scatter(x=df.index, y=df["demand_kw"], name="Site demand", mode="lines",
                             line_shape="hv", line=dict(color="#222222", width=2, dash="dash")))
    fig.update_layout(title=title, height=height, xaxis_title="Time of day",
                      yaxis_title="Active power (kW)<br>(+ = supply, − = absorption)")
    _frame_all_axes(fig)
    return fig


def energy_balance_bars(closure: pd.Series, title: str, height: int = 420) -> go.Figure:
    """Two stacked bars (energy supplied vs energy used) from :func:`energy_closure`."""
    supply = [("Photovoltaic generation", "pv"), ("Grid import", "grid"),
              ("Battery discharge", "battery")]
    use = [("Site demand", "load"), ("Grid export", "grid"), ("Battery charge", "battery")]
    fig = go.Figure()
    for group, items in (("Energy supplied", supply), ("Energy used", use)):
        for name, key in items:
            v = closure[name]
            fig.add_trace(go.Bar(
                x=[group], y=[v], name=name, legendgroup=name,
                marker=dict(color=COMPONENT_COLORS[key], line=dict(color="#222222", width=1),
                            pattern=dict(shape="/" if group == "Energy used" and key != "load"
                                         else "")),
                text=[f"{v:,.0f}" if v > 0 else ""], textposition="inside",
            ))
    fig.update_layout(title=title, height=height, barmode="stack", bargap=0.45,
                      yaxis_title="Energy over the day (kWh)", hovermode="closest")
    return fig


def tradeoff_scatter(df: pd.DataFrame, x: str, y: str, title: str, height: int = 520,
                     x_title: Optional[str] = None, y_title: Optional[str] = None) -> go.Figure:
    """One labelled marker per variant (index) in an x–y trade-off plane."""
    symbols = ["circle", "square", "diamond", "triangle-up", "triangle-down", "x", "star",
               "hexagon"]
    fig = go.Figure()
    for i, (name, row) in enumerate(df.iterrows()):
        st = variant_style(name, i)
        fig.add_trace(go.Scatter(
            x=[row[x]], y=[row[y]], mode="markers", name=name,
            marker=dict(color=st["color"], symbol=symbols[i % len(symbols)], size=13,
                        line=dict(color="#222222", width=1)),
        ))
    fig.update_layout(title=title, height=height, hovermode="closest",
                      xaxis_title=x_title or x, yaxis_title=y_title or y)
    return fig


def save(fig: go.Figure, path, png: bool = False) -> None:
    """Save interactive HTML (and optionally a static PNG via kaleido)."""
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(path.with_suffix(".html"), include_plotlyjs="cdn")
    if png:
        fig.write_image(path.with_suffix(".png"), scale=2)


def format_table(df: pd.DataFrame, digits: int = 2):
    """Pandas Styler with thousands separators and fixed decimals for notebooks."""
    return df.style.format(precision=digits, thousands=",", na_rep="—")


__all__: Iterable[str] = [
    "PALETTE", "COMPONENT_COLORS", "VARIANT_STYLES", "variant_style", "line_comparison",
    "stacked_panels", "grouped_bars", "parity_plot", "tradeoff_scatter", "save", "format_table",
]
