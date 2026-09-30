"""Chart spec rendered three ways: Plotly (app + HTML export), Matplotlib PNG (PDF), native PPTX chart.

One spec -> identical numbers everywhere. Single value axis only (no dual-axis charts): two measures of
different scale are two charts.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

import pandas as pd

# RESP Retention Analyst theme: deep-green accent, brass for targets, then distinct hues. Assign in order.
PALETTE = ["#1F5B3F", "#B08A3C", "#2a78d6", "#e34948", "#4a3aa7", "#1baf7a", "#e87ba4", "#6B7A88"]
INK, INK2, MUTED, GRID, AXIS, SURFACE = "#14202B", "#3E4C59", "#6B7A88", "#E6EAEE", "#D5DBE0", "#FFFFFF"
BRASS, BAD = "#B08A3C", "#B3261E"
KINDS = ("bar", "hbar", "line", "stacked", "pie", "scatter")
FONT = "IBM Plex Sans, system-ui, -apple-system, Segoe UI, sans-serif"


@dataclass
class ChartSpec:
    kind: str
    x: list
    series: dict            # name -> list of y (same length as x)
    title: str = ""
    pct: bool = False       # y values are fractions (0.45 = 45%)
    xlabel: str = ""
    ylabel: str = ""
    target: float | None = None      # reference line (e.g. target %)
    line_series: list = field(default_factory=list)   # kept for compatibility; not used (no dual axes)

    def to_df(self) -> pd.DataFrame:
        d = {self.xlabel or "x": self.x}
        d.update(self.series)
        return pd.DataFrame(d)


def from_df(df: pd.DataFrame, x: str, y: list[str] | str, kind="bar", title="", pct=None, target=None,
            top: int | None = None, line_series=None) -> ChartSpec | None:
    if df is None or df.empty or x not in df.columns:
        return None
    ys = [y] if isinstance(y, str) else list(y)
    ys = [c for c in ys if c in df.columns]
    if not ys:
        return None
    if kind == "combo": kind = "bar"
    d = df.head(top) if top else df
    if pct is None:
        vals = pd.to_numeric(d[ys[0]], errors="coerce").dropna()
        pct = bool(len(vals)) and vals.abs().max() <= 1.5 and any(k in ys[0].lower() for k in ("%", "pct", "ratio", "share", "conv", "ret", "pen"))
    return ChartSpec(kind=kind, x=[str(v) for v in d[x]], title=title or f"{', '.join(ys)} by {x}",
                     series={c: [None if pd.isna(v) else float(v) for v in pd.to_numeric(d[c], errors="coerce")] for c in ys},
                     pct=bool(pct), xlabel=x, target=target)


# --------------------------------------------------------------------------- #
def to_plotly(spec: ChartSpec):
    import plotly.graph_objects as go

    fig = go.Figure()
    vf = ":.1%" if spec.pct else ":,.0f"
    kind = "bar" if spec.kind == "combo" else spec.kind
    n_marks = len(spec.x) * max(1, len(spec.series))
    for i, (name, ys) in enumerate(list(spec.series.items())[:8]):
        c = PALETTE[i]
        if kind == "pie":
            fig.add_trace(go.Pie(labels=spec.x, values=ys, hole=.55, sort=False, marker=dict(colors=PALETTE, line=dict(color=SURFACE, width=2)),
                                 textinfo="percent", hovertemplate="%{label}: %{value:,.0f} (%{percent})<extra></extra>"))
            break
        if kind == "line":
            fig.add_trace(go.Scatter(x=spec.x, y=ys, name=name, mode="lines+markers", line=dict(color=c, width=2),
                                     marker=dict(size=8, line=dict(width=2, color=SURFACE)),
                                     hovertemplate=f"{name}: %{{y{vf}}}<extra></extra>"))
        elif kind == "scatter":
            fig.add_trace(go.Scatter(x=spec.x, y=ys, name=name, mode="markers", marker=dict(color=c, size=9, line=dict(width=2, color=SURFACE))))
        elif kind == "hbar":
            fig.add_trace(go.Bar(y=spec.x, x=ys, name=name, orientation="h", marker=dict(color=c, line=dict(width=0)),
                                 text=[_lab(v, spec.pct) for v in ys] if len(spec.series) == 1 else None,
                                 textposition="outside", cliponaxis=False, textfont=dict(color=INK2, size=11),
                                 hovertemplate=f"%{{y}}<br>{name}: %{{x{vf}}}<extra></extra>"))
        else:
            fig.add_trace(go.Bar(x=spec.x, y=ys, name=name, marker=dict(color=c, line=dict(width=0)),
                                 text=[_lab(v, spec.pct) for v in ys] if n_marks <= 12 and kind != "stacked" else None,
                                 textposition="outside", cliponaxis=False, textfont=dict(color=INK2, size=11),
                                 hovertemplate=f"%{{x}}<br>{name}: %{{y{vf}}}<extra></extra>"))
    if kind == "stacked":
        fig.update_layout(barmode="stack")
    if spec.target is not None and kind != "pie":
        lab = dict(text=f"Target {_lab(spec.target, spec.pct)}", font=dict(color=INK2, size=11))
        if kind == "hbar":
            fig.add_vline(x=spec.target, line_dash="dash", line_color=BRASS, line_width=2, annotation=lab)
        else:
            fig.add_hline(y=spec.target, line_dash="dash", line_color=BRASS, line_width=2, annotation=lab,
                          annotation_position="top left")
    fmt = ".0%" if spec.pct else ",.0f"
    if kind == "hbar":
        fig.update_xaxes(tickformat=fmt); fig.update_yaxes(autorange="reversed")
    elif kind != "pie":
        fig.update_yaxes(tickformat=fmt)
    h = 340 if kind != "hbar" else max(300, 28 * len(spec.x) + 90)
    fig.update_layout(
        title=dict(text=f"<b>{spec.title}</b>", x=0, xanchor="left", font=dict(size=14, color=INK, family="Archivo, " + FONT)),
        height=h, margin=dict(l=8, r=28, t=52, b=8), bargap=0.3, bargroupgap=0.08, barcornerradius=4,
        hovermode="x unified" if kind == "line" else "closest",
        showlegend=len(spec.series) > 1 or kind == "pie",
        legend=dict(orientation="h", y=1.0, x=1, xanchor="right", yanchor="bottom", font=dict(color=INK2, size=11)),
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE, hoverlabel=dict(bgcolor="#ffffff", font_color=INK, bordercolor=GRID),
        font=dict(family=FONT, size=12, color=INK2))
    fig.update_xaxes(showgrid=kind == "hbar", gridcolor=GRID, linecolor=AXIS, tickfont=dict(color=MUTED), zeroline=False)
    fig.update_yaxes(showgrid=kind != "hbar", gridcolor=GRID, linecolor=AXIS, tickfont=dict(color=MUTED), zeroline=False)
    return fig


def _lab(v, pct):
    if v is None or pd.isna(v):
        return ""
    return f"{v * 100:.0f}%" if pct else (f"{v:,.0f}" if abs(v) >= 10 else f"{v:,.1f}")


def to_png(spec: ChartSpec, w=10, h=4.5, dpi=150) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.ticker import FuncFormatter, PercentFormatter

    plt.rcParams["font.family"] = ["DejaVu Sans"]
    fig, ax = plt.subplots(figsize=(w, h), dpi=dpi)
    fig.patch.set_facecolor(SURFACE); ax.set_facecolor(SURFACE)
    x = [str(v)[:22] for v in spec.x]
    series = list(spec.series.items())[:8]
    n = len(series)
    kind = "bar" if spec.kind == "combo" else spec.kind
    if kind == "pie":
        name, ys = series[0]
        ax.pie([0 if v is None else v for v in ys], labels=x, autopct="%1.0f%%", colors=PALETTE, startangle=90,
               wedgeprops=dict(width=.45, edgecolor=SURFACE, linewidth=2), textprops=dict(color=INK2, fontsize=8))
    elif kind == "hbar":
        pos = np.arange(len(x)); bw = .75 / n
        for i, (name, ys) in enumerate(series):
            ax.barh(pos + i * bw, [0 if v is None else v for v in ys], bw * .92, label=name, color=PALETTE[i])
        ax.set_yticks(pos + bw * (n - 1) / 2); ax.set_yticklabels(x, fontsize=8); ax.invert_yaxis()
        if spec.pct: ax.xaxis.set_major_formatter(PercentFormatter(1.0))
        if spec.target is not None: ax.axvline(spec.target, ls="--", color=BRASS, lw=1.5)
    else:
        pos = np.arange(len(x))
        bw = .75 / max(1, n) if kind not in ("stacked",) else .65
        bottom = np.zeros(len(x))
        for i, (name, ys) in enumerate(series):
            yv = np.array([np.nan if v is None else v for v in ys], dtype=float)
            c = PALETTE[i]
            if kind == "line":
                ax.plot(pos, yv, marker="o", lw=2, ms=6, mec=SURFACE, mew=1.5, label=name, color=c)
            elif kind == "scatter":
                ax.scatter(pos, yv, label=name, color=c)
            elif kind == "stacked":
                ax.bar(pos, np.nan_to_num(yv), bw, bottom=bottom, label=name, color=c, edgecolor=SURFACE, linewidth=1)
                bottom += np.nan_to_num(yv)
            else:
                ax.bar(pos + i * bw - .375 + bw / 2, yv, bw * .92, label=name, color=c)
        ax.set_xticks(pos); ax.set_xticklabels(x, rotation=35 if len(x) > 6 else 0, ha="right" if len(x) > 6 else "center", fontsize=8)
        if spec.pct: ax.yaxis.set_major_formatter(PercentFormatter(1.0))
        else: ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
        if spec.target is not None: ax.axhline(spec.target, ls="--", color=BRASS, lw=1.5)
    ax.set_title(spec.title, loc="left", fontsize=12, color=INK, fontweight="bold")
    for s_ in ("top", "right"): ax.spines[s_].set_visible(False)
    for s_ in ("left", "bottom"): ax.spines[s_].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelsize=8)
    if kind != "pie":
        ax.grid(axis="y" if kind != "hbar" else "x", color=GRID, lw=.8); ax.set_axisbelow(True)
    if n > 1:
        ax.legend(fontsize=8, frameon=False, loc="lower right", bbox_to_anchor=(1, 1.0), ncol=min(n, 4), labelcolor=INK2)
    fig.tight_layout()
    buf = io.BytesIO(); fig.savefig(buf, format="png", facecolor=SURFACE); plt.close(fig)
    return buf.getvalue()
