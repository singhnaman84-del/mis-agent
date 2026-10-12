"""Generic dashboards for spreadsheets the app does not know yet.

Any Excel in the Drive folder that matches no known MIS type is still read: the header row is found automatically,
blank rows / columns are dropped, numbers and dates are typed, and the app offers group-by / measure / chart controls.
Nothing here assumes a layout, so a new report type works the day it lands in Drive.
"""
from __future__ import annotations

import os
import re

import pandas as pd

from .charts import ChartSpec
from .report import Report
from .util import iter_sheet, sheet_names

MAX_ROWS = 200_000


def sheets(path: str) -> list[str]:
    try:
        return sheet_names(path)
    except Exception:
        return []


def read_table(path: str, sheet: str) -> pd.DataFrame:
    """Best-guess table of one sheet: the header is the row (in the first 20) with the most text cells; two-row
    headers ('Sep'26' over 'Base / Ach.') are merged."""
    rows = []
    for i, r in enumerate(iter_sheet(path, sheet, max_col=120)):
        rows.append(r)
        if i >= MAX_ROWS: break
    if not rows: return pd.DataFrame()
    w = max(len(r) for r in rows)
    rows = [list(r) + [None] * (w - len(r)) for r in rows]
    def texty(r): return sum(isinstance(v, str) and v.strip() != "" and not re.fullmatch(r"[\d.,%-]+", v.strip()) for v in r)
    h = max(range(min(20, len(rows))), key=lambda i: (texty(rows[i]), -i))
    hdr = [str(v).strip() if v is not None else "" for v in rows[h]]
    if h > 0:                                   # merge a group row above (forward-filled), e.g. month over Base / Ach.
        above, last = [], ""
        for v in rows[h - 1]:
            last = str(v).strip() if v not in (None, "") else last
            above.append(last)
        if sum(bool(a) for a in above) >= 2:
            hdr = [f"{a} {b}".strip() if a and a != b and b else (b or a) for a, b in zip(above, hdr)]
    seen, cols = {}, []
    for j, c in enumerate(hdr):
        c = c or f"Column {j + 1}"
        seen[c] = seen.get(c, 0) + 1
        cols.append(c if seen[c] == 1 else f"{c} ({seen[c]})")
    df = pd.DataFrame(rows[h + 1:], columns=cols)
    df = df.dropna(how="all").dropna(axis=1, how="all")
    df = df.loc[:, [c for c in df.columns if not str(c).startswith("Column ") or df[c].notna().mean() > .2]]
    for c in df.columns:
        s = df[c]
        num = pd.to_numeric(s, errors="coerce")
        if num.notna().sum() >= max(3, .8 * s.notna().sum()):
            df[c] = num
        elif s.map(lambda v: hasattr(v, "year")).mean() > .6:
            df[c] = pd.to_datetime(s, errors="coerce")
        else:
            df[c] = s.map(lambda v: "" if v is None else str(v).strip())
    # drop total rows (a text cell saying total) so sums are not doubled
    txt = [c for c in df.columns if df[c].dtype == object]
    if txt:
        tot = df[txt].apply(lambda r: any(re.fullmatch(r"(grand )?total", str(v).strip(), re.I) for v in r), axis=1)
        df = df[~tot]
    return df.reset_index(drop=True)


def roles(df: pd.DataFrame) -> tuple[list[str], list[str], list[str]]:
    """(dimensions, measures, dates) - dimensions are text columns with a manageable number of values."""
    dims, meas, dates = [], [], []
    for c in df.columns:
        s = df[c]
        if pd.api.types.is_datetime64_any_dtype(s): dates.append(c)
        elif pd.api.types.is_numeric_dtype(s):
            if not re.search(r"\b(code|id|sr|s\.?no|serial|pin|mobile|phone)\b", str(c), re.I): meas.append(c)
        else:
            n = s.replace("", pd.NA).nunique()
            if 1 < n <= max(200, len(s) // 2): dims.append(c)
    return dims, meas, dates


def summarise(df: pd.DataFrame, by: str | None, measure: str | None, agg: str = "sum", top: int = 25) -> pd.DataFrame:
    if df.empty: return df
    if not by:
        return pd.DataFrame([{"Rows": len(df), **({f"{agg} of {measure}": getattr(df[measure], agg)()} if measure else {})}])
    if measure:
        g = df.groupby(by, dropna=False)[measure].agg(agg).reset_index().sort_values(measure, ascending=False)
    else:
        g = df.groupby(by, dropna=False).size().reset_index(name="Rows").sort_values("Rows", ascending=False)
    return g.head(top)


def report(name: str, sheet: str, df: pd.DataFrame, by: str | None, measure: str | None, agg: str, flt: dict | None = None) -> Report:
    rep = Report(f"{os.path.splitext(name)[0]} - {sheet}", f"{len(df):,} rows" + (f" · filtered: {flt}" if flt else ""))
    s = summarise(df, by, measure, agg)
    if by and len(s) > 1:
        y = measure or "Rows"
        rep.chart(ChartSpec("hbar" if len(s) > 6 else "bar", [str(v) for v in s[by]], {f"{agg} of {y}" if measure else "Rows": list(s[y])},
                            f"{(agg + ' of ' + y) if measure else 'Rows'} by {by}"))
    rep.table(s, f"By {by}" if by else "Summary")
    rep.table(df.head(500), "Rows (first 500)")
    return rep
