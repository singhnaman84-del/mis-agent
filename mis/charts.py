"""Chart spec rendered three ways: Plotly (app + HTML export), Matplotlib PNG (PDF), native PPTX chart.

One spec -> identical numbers everywhere.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

import pandas as pd

PALETTE = ["#1F3A5F", "#E4572E", "#29A19C", "#F2A541", "#7B61FF", "#8E9AAF", "#D1495B", "#3C91E6"]
KINDS = ("bar", "hbar", "line", "stacked", "pie", "combo", "scatter")


@dataclass
class ChartSpec:
    kind: str
    x: list
    series: dict            # name -> list of y (same length as x)
    title: str = ""
    pct: bool = False       # y values are fractions (0.45 = 45%)
    xlabel: str = ""
    ylabel: str = ""
    target: float | None = None      # horizontal reference line (e.g. target %)
    line_series: list = field(default_factory=list)   # for combo: names drawn as line on secondary axis

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
    d = df.head(top) if top else df
    if pct is None:
        vals = pd.to_numeric(d[ys[0]], errors="coerce").dropna()
        pct = bool(len(vals)) and vals.abs().max() <= 1.5 and any(k in ys[0].lower() for k in ("%", "pct", "ratio", "share", "conv", "ret", "pen"))
    return ChartSpec(kind=kind, x=[str(v) for v in d[x]], title=title or f"{', '.join(ys)} by {x}",
                     series={c: [None if pd.isna(v) else float(v) for v in pd.to_numeric(d[c], errors="coerce")] for c in ys},
                     pct=bool(pct), xlabel=x, target=target, line_series=list(line_series or []))


# --------------------------------------------------------------------------- #
def to_plotly(spec: ChartSpec):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(specs=[[{"secondary_y": spec.kind == "combo"}]])
    for i, (name, ys) in enumerate(spec.series.items()):
        c = PALETTE[i % len(PALETTE)]
        if spec.kind == "pie":
            fig = go.Figure(go.Pie(labels=spec.x, values=ys, hole=.45, marker=dict(colors=PALETTE)))
            break
        if spec.kind in ("line",) or (spec.kind == "combo" and name in spec.line_series):
            fig.add_trace(go.Scatter(x=spec.x, y=ys, name=name, mode="lines+markers", line=dict(color=c, width=2.5)),
                          secondary_y=spec.kind == "combo")
        elif spec.kind == "scatter":
            fig.add_trace(go.Scatter(x=spec.x, y=ys, name=name, mode="markers", marker=dict(color=c, size=9)))
        elif spec.kind == "hbar":
            fig.add_trace(go.Bar(y=spec.x, x=ys, name=name, orientation="h", marker_color=c,
                                 text=[_lab(v, spec.pct) for v in ys], textposition="auto"))
        else:
            fig.add_trace(go.Bar(x=spec.x, y=ys, name=name, marker_color=c,
                                 text=[_lab(v, spec.pct) for v in ys] if len(spec.x) <= 12 else None, textposition="auto"))
    if spec.kind == "stacked":
        fig.update_layout(barmode="stack")
    if spec.target is not None and spec.kind not in ("pie",):
        if spec.kind == "hbar":
            fig.add_vline(x=spec.target, line_dash="dash", line_color="#D1495B",
                          annotation_text=f"Target {_lab(spec.target, spec.pct)}")
        else:
            fig.add_hline(y=spec.target, line_dash="dash", line_color="#D1495B",
                          annotation_text=f"Target {_lab(spec.target, spec.pct)}")
    fmt = ".0%" if spec.pct else ",.0f"
    if spec.kind == "hbar":
        fig.update_xaxes(tickformat=fmt); fig.update_yaxes(autorange="reversed")
    elif spec.kind != "pie":
        fig.update_yaxes(tickformat=fmt, secondary_y=False)
    h = 360 if spec.kind != "hbar" else max(320, 26 * len(spec.x) + 90)
    fig.update_layout(title=dict(text=spec.title, x=0, font=dict(size=15)), height=h, margin=dict(l=10, r=10, t=50, b=10),
                      legend=dict(orientation="h", y=-0.15), template="plotly_white", font=dict(family="Inter, Arial", size=12))
    return fig


def _lab(v, pct):
    if v is None or pd.isna(v):
        return ""
    return f"{v * 100:.0f}%" if pct else (f"{v:,.0f}" if abs(v) >= 10 else f"{v:,.1f}")


def to_png(spec: ChartSpec, w=10, h=4.5, dpi=150) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter, PercentFormatter

    fig, ax = plt.subplots(figsize=(w, h), dpi=dpi)
    x = [str(v)[:22] for v in spec.x]
    n = len(spec.series)
    if spec.kind == "pie":
        name, ys = next(iter(spec.series.items()))
        ax.pie([0 if v is None else v for v in ys], labels=x, autopct="%1.0f%%", colors=PALETTE, startangle=90,
               wedgeprops=dict(width=.45))
    elif spec.kind == "hbar":
        import numpy as np
        pos = np.arange(len(x)); bw = .8 / n
        for i, (name, ys) in enumerate(spec.series.items()):
            ax.barh(pos + i * bw, [0 if v is None else v for v in ys], bw, label=name, color=PALETTE[i % 8])
        ax.set_yticks(pos + bw * (n - 1) / 2); ax.set_yticklabels(x, fontsize=8); ax.invert_yaxis()
        if spec.pct: ax.xaxis.set_major_formatter(PercentFormatter(1.0))
        if spec.target is not None: ax.axvline(spec.target, ls="--", color="#D1495B", lw=1.2)
    else:
        import numpy as np
        pos = np.arange(len(x))
        ax2 = ax.twinx() if spec.kind == "combo" and spec.line_series else None
        bars = [k for k in spec.series if not (spec.kind == "line" or k in spec.line_series or spec.kind == "scatter")]
        bw = .8 / max(1, len(bars)) if spec.kind != "stacked" else .7
        bottom = np.zeros(len(x)); bi = 0
        for i, (name, ys) in enumerate(spec.series.items()):
            yv = np.array([np.nan if v is None else v for v in ys], dtype=float)
            c = PALETTE[i % 8]
            if spec.kind == "line" or name in spec.line_series:
                (ax2 or ax).plot(pos, yv, marker="o", lw=2.2, label=name, color=c)
            elif spec.kind == "scatter":
                ax.scatter(pos, yv, label=name, color=c)
            elif spec.kind == "stacked":
                ax.bar(pos, np.nan_to_num(yv), bw, bottom=bottom, label=name, color=c); bottom += np.nan_to_num(yv)
            else:
                ax.bar(pos + bi * bw - .4 + bw / 2, yv, bw, label=name, color=c); bi += 1
        ax.set_xticks(pos); ax.set_xticklabels(x, rotation=35 if len(x) > 6 else 0, ha="right" if len(x) > 6 else "center", fontsize=8)
        if spec.pct: ax.yaxis.set_major_formatter(PercentFormatter(1.0))
        else: ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
        if spec.target is not None: ax.axhline(spec.target, ls="--", color="#D1495B", lw=1.2)
    ax.set_title(spec.title, loc="left", fontsize=12, color="#1F3A5F", fontweight="bold")
    for s in ("top", "right"): ax.spines[s].set_visible(False)
    ax.grid(axis="y" if spec.kind != "hbar" else "x", alpha=.25)
    if n > 1 or spec.line_series:
        ax.legend(fontsize=8, frameon=False, loc="upper center", bbox_to_anchor=(.5, -0.18), ncol=min(n, 5))
    fig.tight_layout()
    buf = io.BytesIO(); fig.savefig(buf, format="png"); plt.close(fig)
    return buf.getvalue()
