"""Metrics, rankings, insights and the report builders behind every dashboard.

Conventions
- Percentages are fractions; always sum(ach) / sum(base), never an average of percentages.
- "Closed" month = latest month that is not month-to-date; the running month is shown as a pulse, because a
  mid-month ratio is mechanically low (half the month's policies have not had the chance to renew yet).
- Policies to target = max(0, target x base - ach).
"""
from __future__ import annotations

import math

import pandas as pd

from .charts import ChartSpec, from_df
from .model import OEM_LABEL, OEMS, TARGETS, re_zm
from .report import Report
from .util import month_label, ratio

MIN_BASE = 10          # user-set: dealers below this base are not flagged
AMBER = 0.10           # user-set: Amber band below target (fraction)


def set_criteria(amber: float | None = None, min_base: int | None = None):
    """Critical-dealer definition, set from the app's Settings (same idea as the Retention Analyst's CRIT)."""
    global AMBER, MIN_BASE
    if amber is not None: AMBER = float(amber)
    if min_base is not None: MIN_BASE = int(min_base)


def pct(x) -> str:
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:.1f}%"


def n(x) -> str:
    try:
        return f"{float(x):,.0f}"
    except (TypeError, ValueError):
        return "-"


def inr(x) -> str:
    x = float(x or 0)
    if abs(x) >= 1e7: return f"₹{x / 1e7:,.2f} Cr"
    if abs(x) >= 1e5: return f"₹{x / 1e5:,.1f} L"
    return f"₹{x:,.0f}"


def pts(x) -> str:
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.1f} pts"


# --------------------------------------------------------------------------- #
# core aggregation
# --------------------------------------------------------------------------- #
def rollup(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    """FY / OY / Total base, ach, % per group."""
    if df.empty:
        return pd.DataFrame(columns=by + ["FY base", "FY ach", "FY %", "OY base", "OY ach", "OY %", "Base", "Renewed", "Ret %"])
    p = df.pivot_table(index=by, columns="segment", values=["base", "ach"], aggfunc="sum", fill_value=0)
    out = pd.DataFrame(index=p.index)
    for seg in ("FY", "OY"):
        b = p["base"][seg] if seg in p["base"] else 0
        a = p["ach"][seg] if seg in p["ach"] else 0
        out[f"{seg} base"], out[f"{seg} ach"] = b, a
        out[f"{seg} %"] = [ratio(x, y) for x, y in zip(out[f"{seg} ach"], out[f"{seg} base"])]
    out["Base"] = out["FY base"] + out["OY base"]
    out["Renewed"] = out["FY ach"] + out["OY ach"]
    out["Ret %"] = [ratio(x, y) for x, y in zip(out["Renewed"], out["Base"])]
    return out.reset_index()


def add_target(df: pd.DataFrame, oem: str) -> pd.DataFrame:
    t = TARGETS.get(oem, TARGETS["SKODA"])
    d = df.copy()
    d["Gap vs target"] = d["Ret %"] - t["TOT"]
    d["NOP to target"] = [max(0.0, math.ceil(t["TOT"] * b - a)) if b else 0 for b, a in zip(d["Base"], d["Renewed"])]
    d["RAG"] = [rag(x, t["TOT"]) for x in d["Ret %"]]
    return d


def rag(v, target, amber=None) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)): return "-"
    if v >= target: return "Green"
    if v >= target - (AMBER if amber is None else amber): return "Amber"
    return "Red"


def months(df: pd.DataFrame) -> tuple[str | None, str | None, str | None]:
    """(latest closed month, month before it, running month)."""
    if df.empty: return None, None, None
    closed = sorted(df.loc[~df["is_mtd"].astype(bool), "month"].unique())
    mtd = sorted(df.loc[df["is_mtd"].astype(bool), "month"].unique())
    return (closed[-1] if closed else None, closed[-2] if len(closed) > 1 else None, mtd[-1] if mtd else None)


def oem_frame(pack, oem: str, zm: str | None = None, rm: str | None = None) -> pd.DataFrame:
    r = pack.get("retention")
    if r.empty: return r
    d = r[r["oem"] == oem] if oem != "ALL" else r[r["book"] == "Private car"]
    if zm: d = d[d["zm"] == zm]
    if rm: d = d[d["rm"] == rm]
    return d


# --------------------------------------------------------------------------- #
# insights
# --------------------------------------------------------------------------- #
def retention_insights(d: pd.DataFrame, oem: str, level_name: str = "") -> list[str]:
    out = []
    if d.empty: return out
    last, prev, cur = months(d)
    t = TARGETS.get(oem, TARGETS["SKODA"])
    m = rollup(d, ["month"]).set_index("month")
    if last and last in m.index:
        r = m.loc[last]
        out.append(f"**{month_label(last)} (last closed month)**: retention {pct(r['Ret %'])} on {n(r['Base'])} policies "
                   f"({pct(r['FY %'])} first-year, {pct(r['OY %'])} other-year) vs a {pct(t['TOT'])} target.")
        gap_nop = max(0, math.ceil(t["TOT"] * r["Base"] - r["Renewed"]))
        if gap_nop: out.append(f"Closing {month_label(last)} at target needed **{n(gap_nop)} more renewals**.")
        if prev and prev in m.index:
            dlt = r["Ret %"] - m.loc[prev, "Ret %"]
            out.append(f"Month on month: {pts(dlt)} vs {month_label(prev)} ({pct(m.loc[prev, 'Ret %'])}).")
        fy_gap = r["OY %"] - r["FY %"]
        if not math.isnan(fy_gap) and fy_gap > 0.15:
            out.append(f"First-year customers are the leak: first-year retention trails other-year by {fy_gap * 100:.0f} pts. "
                       f"First-year base is {r['FY base'] / max(1, r['Base']) * 100:.0f}% of all policies due.")
    closed = m.drop(index=[cur] if cur in m.index else [])
    if len(closed) >= 4:
        first, lastv = closed["Ret %"].iloc[:3].mean(), closed["Ret %"].iloc[-3:].mean()
        trend = "improving" if lastv - first > 0.02 else "slipping" if first - lastv > 0.02 else "flat"
        out.append(f"Trend since {month_label(closed.index[0])}: {trend} (first 3 months {pct(first)}, last 3 months {pct(lastv)}).")
    if cur and cur in m.index:
        r = m.loc[cur]
        out.append(f"{month_label(cur)} month-to-date: {pct(r['Ret %'])} on {n(r['Base'])} policies due so far "
                   "(running month - not comparable with closed months).")
    # hierarchy concentration
    if last:
        dl = d[d["month"] == last]
        z = rollup(dl, ["zm"]).query("Base >= @MIN_BASE") if "zm" in dl else pd.DataFrame()
        if len(z) > 1 and not level_name:
            z = z.sort_values("Ret %")
            out.append(f"Weakest zone: **{z.iloc[0]['zm'] or 'unmapped'}** at {pct(z.iloc[0]['Ret %'])}; "
                       f"strongest: **{z.iloc[-1]['zm']}** at {pct(z.iloc[-1]['Ret %'])}.")
        dd = add_target(rollup(dl, ["dealer_code", "dealer_name"]), oem)
        dd = dd[dd["Base"] >= MIN_BASE]
        if len(dd) >= 3:
            red = dd[dd["RAG"] == "Red"]
            top_gap = dd[dd["NOP to target"] > 0].sort_values("NOP to target", ascending=False).head(5)
            share = top_gap["NOP to target"].sum() / max(1, dd["NOP to target"].sum())
            out.append(f"{len(red)} of {len(dd)} dealers (base >= {MIN_BASE}) are Red. The {len(top_gap)} largest gaps hold "
                       f"{share * 100:.0f}% of the renewals needed - "
                       + ", ".join(f"{x['dealer_name']} [{x['dealer_code']}] ({n(x['NOP to target'])})" for _, x in top_gap.iterrows()) + ".")
    return out


def movers(d: pd.DataFrame, a: str, b: str, by=("dealer_code", "dealer_name"), k=8) -> pd.DataFrame:
    x = rollup(d[d["month"] == a], list(by)).set_index(list(by))
    y = rollup(d[d["month"] == b], list(by)).set_index(list(by))
    j = x[["Base", "Ret %"]].join(y[["Base", "Ret %"]], lsuffix=f" {month_label(a)}", rsuffix=f" {month_label(b)}", how="inner")
    j = j[(j[f"Base {month_label(a)}"] >= MIN_BASE) & (j[f"Base {month_label(b)}"] >= MIN_BASE)]
    j["Change (pts)"] = (j[f"Ret % {month_label(b)}"] - j[f"Ret % {month_label(a)}"]) * 100
    return j.reset_index().sort_values("Change (pts)")


# --------------------------------------------------------------------------- #
# report builders
# --------------------------------------------------------------------------- #
def overview_report(pack) -> Report:
    rep = Report("MIS Overview - all OEMs", _asof_line(pack))
    tiles, rows = [], []
    for oem in OEMS:
        s = oem_snapshot(pack, oem)
        if not s: continue
        tiles.append((OEM_LABEL[oem], pct(s["value"]), s["note"]))
        rows.append({"OEM": OEM_LABEL[oem], "Period": s["period"], "Base": s["base"], "Renewed": s["ach"],
                     "Ret %": s["value"], "FY %": s.get("fy"), "OY %": s.get("oy"), "Target %": s["target"],
                     "Gap (pts)": (s["value"] - s["target"]) * 100 if s["value"] == s["value"] else None,
                     "Running month %": s.get("mtd")})
    rep.kpis(tiles, "Retention by OEM (last closed month; RE = month to date)")
    tb = pd.DataFrame(rows)
    rep.table(tb, "OEM scorecard")
    if len(tb):
        rep.chart(ChartSpec("bar", list(tb["OEM"]), {"Retention %": list(tb["Ret %"]), "Target": list(tb["Target %"])},
                            "Retention vs target by OEM", pct=True))
    # private-car monthly trend by OEM
    r = pack.get("retention")
    pc = r[r["book"] == "Private car"]
    if not pc.empty:
        t = rollup(pc, ["month", "oem"])
        piv = t.pivot(index="month", columns="oem", values="Ret %").sort_index()
        rep.chart(ChartSpec("line", [month_label(m) for m in piv.index],
                            {OEM_LABEL.get(c, c): list(piv[c]) for c in piv.columns}, "Private car - retention % by month", pct=True))
    rep.h("Headlines")
    bl = []
    for oem in OEMS:
        ins = retention_insights(oem_frame(pack, oem), oem) if oem != "ROYAL ENFIELD" else re_headlines(pack)
        if ins: bl.append(f"**{OEM_LABEL[oem]}** - " + " ".join(i.replace("**", "") for i in ins[:2]))
    rep.bullets(bl)
    pay = payouts_summary(pack)
    if pay is not None: rep.table(pay, "Payouts pending by book")
    return rep


def oem_snapshot(pack, oem: str) -> dict | None:
    t = TARGETS.get(oem, TARGETS["SKODA"])
    if oem == "ROYAL ENFIELD":
        rg = pack.get("re_region")
        if rg.empty: return None
        fb, fa, ob, oa = rg["fy_base"].sum(), rg["fy_ach"].sum(), rg["oy_base"].sum(), rg["oy_ach"].sum()
        return {"value": ratio(fa + oa, fb + ob), "fy": ratio(fa, fb), "oy": ratio(oa, ob), "base": fb + ob, "ach": fa + oa,
                "period": "MTD " + pack.asof.get("Royal Enfield", ""), "target": t["TOT"],
                "note": f"FY {pct(ratio(fa, fb))} · OY {pct(ratio(oa, ob))}", "mtd": ratio(fa + oa, fb + ob)}
    d = oem_frame(pack, oem)
    if d.empty: return None
    last, prev, cur = months(d)
    m = rollup(d, ["month"]).set_index("month")
    if not last: last = cur
    r = m.loc[last]
    out = {"value": r["Ret %"], "fy": r["FY %"], "oy": r["OY %"], "base": r["Base"], "ach": r["Renewed"],
           "period": month_label(last), "target": t["TOT"], "note": f"{month_label(last)} · FY {pct(r['FY %'])} · OY {pct(r['OY %'])}"}
    if cur and cur in m.index: out["mtd"] = m.loc[cur, "Ret %"]
    return out


def oem_report(pack, oem: str, zm: str | None = None, rm: str | None = None, extras: bool = True) -> Report:
    if oem == "ROYAL ENFIELD":
        return re_report(pack, zm=zm, rm=rm)
    label = OEM_LABEL.get(oem, oem) if oem != "ALL" else "Private car (all brands)"
    scope = " / ".join(x for x in (zm, rm) if x)
    rep = Report(f"{label} retention dashboard" + (f" - {scope}" if scope else ""), _asof_line(pack, "Private car"))
    d = oem_frame(pack, oem, zm, rm)
    if d.empty:
        return rep.p("No retention data for this selection in the current drop.")
    tgt = TARGETS.get(oem, TARGETS["SKODA"])
    last, prev, cur = months(d)
    m = rollup(d, ["month"]).sort_values("month")
    lr = m.set_index("month").loc[last] if last else None
    tiles = []
    if lr is not None:
        tiles += [(f"{month_label(last)} retention", pct(lr["Ret %"]), f"target {pct(tgt['TOT'])}"),
                  ("First year", pct(lr["FY %"]), f"{n(lr['FY ach'])} / {n(lr['FY base'])}"),
                  ("Other year", pct(lr["OY %"]), f"{n(lr['OY ach'])} / {n(lr['OY base'])}"),
                  ("Renewals to target", n(max(0, math.ceil(tgt['TOT'] * lr['Base'] - lr['Renewed']))), month_label(last))]
    if cur:
        cr = m.set_index("month").loc[cur]
        tiles.append((f"{month_label(cur)} MTD", pct(cr["Ret %"]), f"{n(cr['Renewed'])} / {n(cr['Base'])} due so far"))
    ytd = m[m["month"] != cur]
    if len(ytd):
        tiles.append(("YTD (closed months)", pct(ratio(ytd["Renewed"].sum(), ytd["Base"].sum())), f"{n(ytd['Base'].sum())} policies"))
    rep.kpis(tiles)
    rep.h("Insights")
    rep.bullets(retention_insights(d, oem, scope))
    rep.h("Monthly trend")
    mm = m.assign(Month=[month_label(x) + (" (MTD)" if x == cur else "") for x in m["month"]])
    rep.chart(ChartSpec("line", list(mm["Month"]), {"First year": list(mm["FY %"]), "Other year": list(mm["OY %"]),
                                                    "Total": list(mm["Ret %"])}, f"{label} - retention % by month", pct=True,
                        target=tgt["TOT"]))
    rep.chart(ChartSpec("stacked", list(mm["Month"]), {"Renewed": list(mm["Renewed"]),
                                                       "Not renewed": list(mm["Base"] - mm["Renewed"])},
                        f"{label} - policies due vs renewed"))
    rep.table(mm[["Month", "FY base", "FY ach", "FY %", "OY base", "OY ach", "OY %", "Base", "Renewed", "Ret %"]], "Month-wise")
    if last:
        dl = d[d["month"] == last]
        if not zm and not rm:
            z = add_target(rollup(dl, ["zm"]), oem).sort_values("Ret %")
            rep.h(f"Zones (ZM) - {month_label(last)}")
            rep.chart(from_df(z, "zm", "Ret %", "hbar", f"Retention % by ZM - {month_label(last)}", pct=True, target=tgt["TOT"]))
            rep.table(z.rename(columns={"zm": "ZM"})[["ZM", "Base", "Renewed", "Ret %", "FY %", "OY %", "NOP to target", "RAG"]])
        if not rm:
            r_ = add_target(rollup(dl, ["zm", "rm"]), oem).sort_values("Ret %")
            rep.h(f"Relationship managers - {month_label(last)}")
            rep.chart(from_df(r_[r_["Base"] >= MIN_BASE], "rm", "Ret %", "hbar", f"Retention % by RM - {month_label(last)}",
                              pct=True, target=tgt["TOT"]))
            rep.table(r_.rename(columns={"zm": "ZM", "rm": "RM"})[["ZM", "RM", "Base", "Renewed", "Ret %", "FY %", "OY %",
                                                                    "NOP to target", "RAG"]])
        dd = add_target(rollup(dl, ["dealer_code", "dealer_name", "rm", "zm"]), oem)
        dd = dd[dd["Base"] >= MIN_BASE]
        rep.h(f"Dealers - {month_label(last)}")
        worst = dd.sort_values("NOP to target", ascending=False).head(15)
        rep.chart(from_df(worst, "dealer_name", "NOP to target", "hbar", "Largest gaps to target (renewals needed)", pct=False))
        rep.table(worst.rename(columns={"dealer_code": "Code", "dealer_name": "Dealer", "rm": "RM", "zm": "ZM"})[
            ["Code", "Dealer", "RM", "ZM", "Base", "Renewed", "Ret %", "FY %", "OY %", "NOP to target", "RAG"]],
            "Priority dealers (largest renewal gap)")
        best = dd.sort_values("Ret %", ascending=False).head(10)
        rep.table(best.rename(columns={"dealer_code": "Code", "dealer_name": "Dealer", "rm": "RM", "zm": "ZM"})[
            ["Code", "Dealer", "RM", "ZM", "Base", "Renewed", "Ret %"]], "Top performers")
        if prev:
            mv = movers(d, prev, last)
            if len(mv):
                rep.table(mv.head(8), f"Biggest decliners {month_label(prev)} -> {month_label(last)}")
                rep.table(mv.tail(8).iloc[::-1], f"Biggest improvers {month_label(prev)} -> {month_label(last)}")
    # extras by OEM (the OEM pages show penetration / open cases / payouts on their own tabs)
    if not extras:
        return rep
    if oem in ("AUDI", "SKODA", "VOLKSWAGEN", "PORSCHE", "ALL"):
        rep.extend(penetration_section(pack, oem, zm, rm))
    if oem == "VOLVO":
        rep.extend(volvo_open_section(pack, zm, rm))
    rep.extend(payout_section(pack, "pc_payouts", None if oem == "ALL" else oem, zm, rm))
    return rep


def penetration_section(pack, oem, zm=None, rm=None) -> Report:
    rep = Report("")
    p = pack.get("pc_penetration_month")
    if p.empty: return rep
    d = p if oem == "ALL" else p[p["oem"] == oem]
    if zm: d = d[d["zm"] == zm]
    if rm: d = d[d["rm"] == rm]
    if d.empty: return rep
    rep.h("New-car penetration (policies at sale vs retail)")
    pending = d[~d["misp_status"].str.lower().eq("created")]
    d = d[d["misp_status"].str.lower().eq("created")]
    if d.empty: return rep
    m = d.groupby("month", as_index=False)[["retail", "new_nop", "renewal_base", "renewed"]].sum().sort_values("month")
    m["Penetration %"] = [ratio(a, b) for a, b in zip(m["new_nop"], m["retail"])]
    m["Renewal ret %"] = [ratio(a, b) for a, b in zip(m["renewed"], m["renewal_base"])]
    m["Month"] = [month_label(x) for x in m["month"]]
    rep.chart(ChartSpec("bar", list(m["Month"]), {"Retail": list(m["retail"]), "New policies": list(m["new_nop"])},
                        "Cars retailed vs new policies written"))
    rep.chart(ChartSpec("line", list(m["Month"]), {"Penetration %": list(m["Penetration %"])}, "Penetration % by month", pct=True))
    rep.table(m[["Month", "retail", "new_nop", "Penetration %", "renewal_base", "renewed", "Renewal ret %"]].rename(
        columns={"retail": "Retail", "new_nop": "New NOP", "renewal_base": "Renewal base", "renewed": "Renewed"}))
    last = m["month"].max()
    dl = d[d["month"] == last].groupby(["dealer_code", "dealer_name", "rm", "zm"], as_index=False)[["retail", "new_nop"]].sum()
    dl["Penetration %"] = [ratio(a, b) for a, b in zip(dl["new_nop"], dl["retail"])]
    dl["Missed policies"] = (dl["retail"] - dl["new_nop"]).clip(lower=0)
    zero = dl[(dl["retail"] >= 5) & (dl["new_nop"] == 0)].sort_values("retail", ascending=False)
    pl = pending[pending["month"] == last]
    bl = [f"{month_label(last)} penetration at onboarded dealers {pct(ratio(dl['new_nop'].sum(), dl['retail'].sum()))} on "
          f"{n(dl['retail'].sum())} cars retailed; {n(dl['Missed policies'].sum())} cars left without a programme policy."]
    if len(zero):
        bl.append(f"{len(zero)} onboarded dealer codes retailed 5+ cars but wrote no new policy: "
                  + ", ".join(f"{a} ({c})" for a, c in zip(zero["dealer_name"].head(6), zero["dealer_code"].head(6))) + ".")
    if len(pl) and pl["retail"].sum():
        bl.append(f"Onboarding opportunity: {pl['dealer_code'].nunique()} dealer codes not yet onboarded (MISP pending) "
                  f"retailed {n(pl['retail'].sum())} cars in {month_label(last)}.")
    rep.bullets(bl)
    rep.table(dl.sort_values("Missed policies", ascending=False).head(12).rename(
        columns={"dealer_code": "Code", "dealer_name": "Dealer", "rm": "RM", "zm": "ZM", "retail": "Retail", "new_nop": "New NOP"}),
        f"Largest penetration gaps - {month_label(last)}")
    return rep


def volvo_open_section(pack, zm=None, rm=None) -> Report:
    rep = Report("")
    oc = pack.get("volvo_open_cases")
    if oc.empty: return rep
    d = oc
    if zm: d = d[d["zm"] == zm]
    if rm: d = d[d["rm"] == rm]
    if d.empty: return rep
    rep.h("Volvo - expired and not renewed (chase list)")
    rep.kpis([("Open cases", n(len(d)), "expired this month"), ("Premium at risk", inr(d["total_premium"].sum()), "total premium"),
              ("First year", n((d["segment"] == "FY").sum()), ""), ("With claims", n((d["claims"] > 0).sum()), "policies")])
    g = d.groupby(["dealer_code", "dealer_name", "rm", "zm"], as_index=False).agg(
        cases=("total_premium", "size"), premium=("total_premium", "sum"), first_year=("segment", lambda x: (x == "FY").sum()))
    rep.table(g.sort_values("premium", ascending=False).rename(columns={"dealer_code": "Code", "dealer_name": "Dealer",
              "rm": "RM", "zm": "ZM", "cases": "Cases", "premium": "Premium at risk", "first_year": "First-year"}), "By dealer")
    ins = d.groupby("insurer", as_index=False).agg(cases=("total_premium", "size"), premium=("total_premium", "sum"))
    rep.chart(from_df(ins.sort_values("cases", ascending=False), "insurer", "cases", "bar", "Open cases by current insurer"))
    wk = d.assign(week=d["expiry_date"].dt.isocalendar().week.astype("Int64")).groupby("week", as_index=False).size()
    if len(wk): rep.chart(from_df(wk, "week", "size", "bar", "Open cases by expiry week"))
    return rep


def payouts_summary(pack) -> pd.DataFrame | None:
    rows = []
    for t, book in (("re_payouts", "Royal Enfield"), ("pc_payouts", "Private car")):
        d = pack.get(t)
        if d.empty: continue
        pend = d[~d["paid"]]
        rows.append({"Book": book, "Invoices pending": len(pend), "Pending value": pend["base_value"].sum(),
                     "Paid invoices": int(d["paid"].sum()), "Paid value": d.loc[d["paid"], "net_amount"].sum()})
    return pd.DataFrame(rows) if rows else None


def payout_section(pack, table, oem=None, zm=None, rm=None) -> Report:
    rep = Report("")
    d = pack.get(table)
    if d.empty: return rep
    if oem: d = d[d["oem"] == oem]
    if zm: d = d[d["zm"] == zm]
    if rm: d = d[d["rm"] == rm]
    pend = d[~d["paid"]]
    if pend.empty: return rep
    rep.h("Dealer payouts pending")
    st = pend.groupby("status", as_index=False).agg(Invoices=("base_value", "size"), Value=("base_value", "sum")).sort_values("Value", ascending=False)
    rep.kpis([("Pending invoices", n(len(pend)), ""), ("Pending value", inr(pend["base_value"].sum()), "base value"),
              ("Paid to date", inr(d.loc[d["paid"], "net_amount"].sum()), f"{n(d['paid'].sum())} invoices")])
    rep.table(st.rename(columns={"status": "Status"}), "Pending by status")
    by = pend.groupby(["dealer_code", "dealer_name", "rm"], as_index=False).agg(Invoices=("base_value", "size"),
                                                                             Value=("base_value", "sum"))
    rep.table(by.sort_values("Value", ascending=False).head(12).rename(columns={"dealer_code": "Code", "dealer_name": "Dealer",
                                                                                 "rm": "RM"}), "Dealers with the most pending")
    return rep


# --------------------------------------------------------------------------- #
# Royal Enfield
# --------------------------------------------------------------------------- #
def re_headlines(pack) -> list[str]:
    out = []
    rg = pack.get("re_region")
    t = TARGETS["ROYAL ENFIELD"]
    if not rg.empty:
        fb, fa, ob, oa = rg["fy_base"].sum(), rg["fy_ach"].sum(), rg["oy_base"].sum(), rg["oy_ach"].sum()
        fp, fpa = rg["fy_base_prev"].sum(), rg["fy_ach_prev"].sum()
        out.append(f"First-year conversion {pct(ratio(fa, fb))} ({n(fa)} of {n(fb)}) vs {pct(ratio(fpa, fp))} on the same "
                   f"date last month ({pts(ratio(fa, fb) - ratio(fpa, fp))}); other-year {pct(ratio(oa, ob))}.")
        out.append(f"At the {pct(t['FY'])} first-year target the month needs {n(max(0, math.ceil(t['FY'] * fb - fa)))} more first-year renewals.")
    ai = pack.get("re_ai_dealer")
    if not ai.empty and not rg.empty:
        c = ai["conversions"].sum()
        out.append(f"AI calling converted {n(c)} policies this month = {pct(ratio(c, rg['fy_ach'].sum()))} of all first-year "
                   f"renewals ({n(ai['calls'].sum())} calls, {pct(ratio(c, ai['disposed'].sum()))} of disposed calls convert).")
    return out


def re_report(pack, zm: str | None = None, rm: str | None = None) -> Report:
    t = TARGETS["ROYAL ENFIELD"]
    scope = " / ".join(x for x in (zm, rm) if x)
    rep = Report("Royal Enfield RESP dashboard" + (f" - {scope}" if scope else ""), _asof_line(pack, "Royal Enfield"))
    rg = pack.get("re_region")
    snap = pack.get("re_dealer_snapshot")
    if zm and not rg.empty:
        rg = rg[[re_zm(x) == zm for x in rg["region"]]]
    if not snap.empty:
        if zm: snap = snap[snap["zm"] == zm]
        if rm: snap = snap[snap["rm"] == rm]
    if not rg.empty and not rm:
        fb, fa, ob, oa = rg["fy_base"].sum(), rg["fy_ach"].sum(), rg["oy_base"].sum(), rg["oy_ach"].sum()
        fp, fpa = rg["fy_base_prev"].sum(), rg["fy_ach_prev"].sum()
        rep.kpis([("First-year conversion", pct(ratio(fa, fb)), f"{n(fa)} / {n(fb)} · target {pct(t['FY'])}"),
                  ("vs same date last month", pts(ratio(fa, fb) - ratio(fpa, fp)), f"was {pct(ratio(fpa, fp))}"),
                  ("Other-year retention", pct(ratio(oa, ob)), f"{n(oa)} / {n(ob)} · target {pct(t['OY'])}"),
                  ("Overall", pct(ratio(fa + oa, fb + ob)), f"target {pct(t['TOT'])}"),
                  ("FY renewals to target", n(max(0, math.ceil(t['FY'] * fb - fa))), "this month")])
    elif not snap.empty:
        fb, fa = snap["fy_base"].sum(), snap["fy_ach"].sum()
        rep.kpis([("First-year conversion", pct(ratio(fa, fb)), f"{n(fa)} / {n(fb)}"),
                  ("Dealers", n(len(snap)), ""), ("FY renewals to target", n(max(0, math.ceil(t['FY'] * fb - fa))), "")])
    rep.h("Insights"); rep.bullets(re_headlines(pack) if not scope else re_scope_insights(snap, t))
    if not rg.empty and not rm:
        rep.h("Regions")
        r = rg.assign(**{"FY %": [ratio(a, b) for a, b in zip(rg["fy_ach"], rg["fy_base"])],
                         "FY % last month": [ratio(a, b) for a, b in zip(rg["fy_ach_prev"], rg["fy_base_prev"])],
                         "OY %": [ratio(a, b) for a, b in zip(rg["oy_ach"], rg["oy_base"])],
                         "Total %": [ratio(a, b) for a, b in zip(rg["tot_ach"], rg["tot_base"])]})
        r["FY change (pts)"] = (r["FY %"] - r["FY % last month"]) * 100
        rep.chart(ChartSpec("bar", list(r["region"]), {"FY % now": list(r["FY %"]), "FY % last month": list(r["FY % last month"])},
                            "First-year conversion by region (same date comparison)", pct=True, target=t["FY"]))
        rep.table(r[["zone", "region", "state", "fy_base", "fy_ach", "FY %", "FY % last month", "FY change (pts)", "oy_base",
                     "oy_ach", "OY %", "Total %", "ai_dealers"]].rename(columns={"zone": "Zone", "region": "Region", "state": "State",
                     "fy_base": "FY base", "fy_ach": "FY ach", "oy_base": "OY base", "oy_ach": "OY ach", "ai_dealers": "AI dealers"}))
    dly = pack.get("re_daily")
    if not dly.empty and not rm:
        dd = dly if rg.empty else dly[dly["region"].isin(rg["region"])]
        dd = dd.groupby("date", as_index=False)["renewals"].sum()
        rep.chart(ChartSpec("bar", [x.strftime("%d %b") for x in dd["date"]], {"Renewals": list(dd["renewals"])},
                            "Renewals per day (FY + OY)"))
    # AI calling
    ai, aid = pack.get("re_ai_dealer"), pack.get("re_ai_daily")
    if not ai.empty:
        rep.h("AI calling")
        a = ai if snap.empty or not scope else ai[ai["dealer_code"].isin(snap["dealer_code"])]
        rep.kpis([("Calls", n(a["calls"].sum()), ""), ("Answered & disposed", n(a["disposed"].sum()), pct(ratio(a['disposed'].sum(), a['calls'].sum())) + " of calls"),
                  ("Conversions", n(a["conversions"].sum()), pct(ratio(a['conversions'].sum(), a['disposed'].sum())) + " of disposed"),
                  ("Share of FY renewals", pct(ratio(a["conversions"].sum(), snap["fy_ach"].sum() if scope and not snap.empty else rg["fy_ach"].sum())), "")])
        if not aid.empty and not scope:
            rep.chart(ChartSpec("line", [x.strftime("%d %b") for x in aid["date"]],
                                {"Conversions (this month)": list(aid["conversions"]), "Conversions (same day last month)": list(aid["conv_prev"])},
                                "AI conversions per day vs last month"))
        low = a[a["fy_base"] >= 100].assign(**{"Conv/base": lambda x: x["conversions"] / x["fy_base"],
                                               "Calls/base": lambda x: x["calls"] / x["fy_base"]}).sort_values("Calls/base").head(12)
        rep.table(low.rename(columns={"dealer_code": "Code", "dealer_name": "Dealer", "region": "Region", "fy_base": "FY base",
                                      "calls": "Calls", "disposed": "Disposed", "conversions": "Conversions"})[
            ["Code", "Dealer", "Region", "FY base", "Calls", "Calls/base", "Disposed", "Conversions", "Conv/base"]],
            "High-base dealers with the least AI calling coverage")
    # dealers
    if not snap.empty:
        from . import ra
        rep.kpis(ra.revenue_lines(snap["fy_base"].sum(), snap["fy_ach"].sum(), t["FY"], ra.RS_MANAGER),
                 f"{'RM' if rm else 'ZM' if zm else 'National'} revenue at ₹{ra.RS_MANAGER} per retained first-year policy (approximate)")
        rep.h("Dealer priorities (first year)")
        s2 = snap.assign(**{"FY %": [ratio(a, b) for a, b in zip(snap["fy_ach"], snap["fy_base"])],
                            "NOP to FY target": [max(0, math.ceil(t["FY"] * b - a)) for a, b in zip(snap["fy_ach"], snap["fy_base"])]})
        pri = s2[s2["fy_base"] >= 50].sort_values("NOP to FY target", ascending=False).head(20)
        rep.chart(from_df(pri.head(15), "dealer_name", "NOP to FY target", "hbar", "Largest first-year gaps to target"))
        rep.table(pri.rename(columns={"dealer_code": "Code", "dealer_name": "Dealer", "rm": "RM", "region": "Region",
                                      "fy_base": "FY base", "fy_ach": "FY ach", "ai_conversions": "AI conv.",
                                      "tier": "Incentive tier", "last_visit": "Last visit"})[
            [c for c in ["Code", "Dealer", "Region", "RM", "FY base", "FY ach", "FY %", "NOP to FY target", "AI conv.",
                         "Incentive tier", "Last visit"] if c in pri.rename(columns={"dealer_code": "Code", "dealer_name": "Dealer",
                         "rm": "RM", "region": "Region", "fy_base": "FY base", "fy_ach": "FY ach", "ai_conversions": "AI conv.",
                         "tier": "Incentive tier", "last_visit": "Last visit"}).columns]], "Priority dealers")
        rmv = s2.groupby(["zm", "rm"], as_index=False)[["fy_base", "fy_ach"]].sum()
        rmv["FY %"] = [ratio(a, b) for a, b in zip(rmv["fy_ach"], rmv["fy_base"])]
        rmv["NOP to FY target"] = [max(0, math.ceil(t["FY"] * b - a)) for a, b in zip(rmv["fy_ach"], rmv["fy_base"])]
        if not rm:
            rep.chart(from_df(rmv.sort_values("FY %"), "rm", "FY %", "hbar", "First-year conversion by RM", pct=True, target=t["FY"]))
            rep.table(rmv.sort_values("FY %").rename(columns={"zm": "ZM", "rm": "RM", "fy_base": "FY base", "fy_ach": "FY ach"}), "RM view")
        # incentive
        rep.h("Dealer incentive scheme")
        q = s2["tier"].value_counts().reindex([0, 5, 10, 15, 20], fill_value=0)
        rep.kpis([("Qualified (any tier)", n((s2["tier"] > 0).sum()), f"of {n(len(s2))} dealers"),
                  ("Incentive earned", inr(s2["incentive_earned"].sum()), "at current achievement"),
                  ("Close to next tier", n(((s2["nop_to_next_tier"] > 0) & (s2["nop_to_next_tier"] <= 5)).sum()), "need 5 or fewer policies")])
        rep.chart(ChartSpec("bar", ["Not qualified", "+5 pts", "+10 pts", "+15 pts", "+20 pts"], {"Dealers": list(q.values)},
                            "Dealers by incentive tier reached"))
        near = s2[(s2["nop_to_next_tier"] > 0)].sort_values("nop_to_next_tier").head(15)
        rep.table(near.rename(columns={"dealer_code": "Code", "dealer_name": "Dealer", "rm": "RM", "tier": "Tier now",
                                       "next_tier": "Next tier (+pts)", "nop_to_next_tier": "Policies needed"})[
            ["Code", "Dealer", "RM", "Tier now", "Next tier (+pts)", "Policies needed"]], "Quick wins: closest to the next tier")
    # focus, visits, penetration, payouts
    foc = pack.get("re_focus")
    if not foc.empty and not rm:
        f = foc if not zm else foc[foc["zm"] == zm]
        rep.h("Focus dealers (high base, low conversion)")
        rep.kpis([("Focus dealers", n(len(f)), ""), ("Conversion now", pct(ratio(f['ach'].sum(), f['base'].sum())),
                   f"was {pct(ratio(f['ach_prev'].sum(), f['base_prev'].sum()))}"),
                  ("Actions logged", n(f["actions_logged"].sum()), "in the tracker")])
    vis = pack.get("re_visits")
    if not vis.empty:
        v = vis[vis["visit_type"] == "Dealership Visit"]
        if zm: v = v[v["rm"].isin(snap["rm"].unique())] if not snap.empty else v
        if rm: v = v[v["rm"] == rm]
        if len(v):
            rep.h("Field visits")
            vm = v.assign(Month=v["date"].dt.strftime("%Y-%m")).groupby("Month", as_index=False).size()
            rep.chart(ChartSpec("bar", [month_label(m) for m in vm["Month"]], {"Visits": list(vm["size"])}, "Dealer visits per month"))
            last_m = v["date"].dt.strftime("%Y-%m").max()
            vr = v[v["date"].dt.strftime("%Y-%m") == last_m].groupby("rm", as_index=False).agg(
                Visits=("dealer_code", "size"), Dealers=("dealer_code", "nunique"), Met_DP_GM=("met_dp_gm", "sum"))
            rep.table(vr.sort_values("Visits").rename(columns={"rm": "RM", "Met_DP_GM": "Met DP/GM"}),
                      f"Visits by RM - {month_label(last_m)} (report as of {v['date'].max():%d %b})")
    pen = pack.get("re_penetration")
    if not pen.empty:
        p = pen if not rm else pen[pen["rm"] == rm]
        if zm: p = p[[re_zm(x) == zm for x in p["region"]]]
        if len(p):
            rep.h("New-vehicle penetration")
            rep.kpis([("Penetration MTD", pct(ratio(p['prog_mtd'].sum(), p['veh_mtd'].sum())), f"{n(p['prog_mtd'].sum())} policies / {n(p['veh_mtd'].sum())} bikes"),
                      ("Penetration YTD", pct(ratio(p['prog_ytd'].sum(), p['veh_ytd'].sum())), ""),
                      ("Add-on share MTD", pct(ratio(p['addon_mtd'].sum(), p['nop_mtd'].sum())), "")])
            mix = {k: p[k].sum() for k in ("essential", "advance", "premium", "no_addon")}
            rep.chart(ChartSpec("pie", ["Essential", "Advance", "Premium", "No add-on"], {"Policies": list(mix.values())}, "Cover mix this month"))
    rep.extend(payout_section(pack, "re_payouts", None, zm, rm))
    return rep


def re_scope_insights(snap, t) -> list[str]:
    if snap.empty: return []
    fb, fa = snap["fy_base"].sum(), snap["fy_ach"].sum()
    out = [f"First-year conversion {pct(ratio(fa, fb))} on {n(fb)} policies due; {n(max(0, math.ceil(t['FY'] * fb - fa)))} renewals to the {pct(t['FY'])} target."]
    if "ai_conversions" in snap:
        out.append(f"AI calling delivered {n(snap['ai_conversions'].sum())} of {n(fa)} first-year renewals.")
    out.append(f"{n((snap['tier'] > 0).sum())} dealers have qualified for an incentive tier; "
               f"{n(((snap['nop_to_next_tier'] > 0) & (snap['nop_to_next_tier'] <= 5)).sum())} are 5 or fewer policies from the next one.")
    return out


# --------------------------------------------------------------------------- #
# entity reports
# --------------------------------------------------------------------------- #
def dealer_report(pack, dealer_code: str) -> Report:
    dc = dealer_code.strip().upper()
    r = pack.get("retention")
    d = r[r["dealer_code"] == dc]
    snap = pack.get("re_dealer_snapshot")
    s_ = snap[snap["dealer_code"] == dc] if not snap.empty else snap
    name = (d["dealer_name"].iloc[0] if len(d) else s_["dealer_name"].iloc[0] if len(s_) else dc)
    rep = Report(f"Dealer review - {name} ({dc})", _asof_line(pack))
    if len(d):
        oem = d["oem"].iloc[0]
        t = TARGETS.get(oem, TARGETS["SKODA"])
        m = rollup(d, ["month"]).sort_values("month")
        _, _, cur = months(d)
        m["Month"] = [month_label(x) + (" (MTD)" if x == cur else "") for x in m["month"]]
        rep.kpis([("OEM", OEM_LABEL.get(oem, oem), ""), ("RM", d["rm"].iloc[0] or "-", ""), ("ZM", d["zm"].iloc[0] or "-", ""),
                  ("YTD retention", pct(ratio(m["Renewed"].sum(), m["Base"].sum())), f"target {pct(t['TOT'])}")])
        rep.bullets(retention_insights(d, oem, dc))
        rep.chart(ChartSpec("line", list(m["Month"]), {"First year": list(m["FY %"]), "Other year": list(m["OY %"]),
                                                       "Total": list(m["Ret %"])}, "Retention % by month", pct=True, target=t["TOT"]))
        rep.table(m[["Month", "FY base", "FY ach", "FY %", "OY base", "OY ach", "OY %", "Base", "Renewed", "Ret %"]], "Month-wise")
        # similar-base peers (same OEM + zone, base ±30% -> ±50% -> whole zone) and the consolidated group
        from . import ra
        book = ra.dealer_book(pack, oem=oem)
        pr = ra.peers(book, dc)
        if pr.get("n"):
            me = book[book["Code"] == dc].iloc[0]
            rep.h(f"Peer standing - {me['Period']}")
            rep.kpis([("This dealer", pct(me["Ret %"]), f"rank {pr['rank']} of {pr['n'] + 1}"),
                      ("Peer median", pct(pr["median"]), f"{pr['n']} peers, base {pr['band']}"),
                      ("Best in band", pct(pr["best"]), f"{pr['best_name']} · base {n(pr['best_base'])}")])
            pl = pr["list"][["Code", "Dealer", "RM", "Base", "Renewed", "Ret %", "RAG"]].copy()
            pl.insert(0, "Rank", range(1, len(pl) + 1))
            rep.table(pl, f"Similar-base peers ({pr['band']})")
        g = ra.group_of(book[book["Code"] == dc].iloc[0]) if (book["Code"] == dc).any() else None
        if g:
            gm = ra.group_members(book, g)
            if len(gm) > 1:
                gb, ga = gm["Base"].sum(), gm["Renewed"].sum()
                rep.h(f"{g['name']} - consolidated across {len(gm)} codes")
                rep.kpis([("Group retention", pct(ratio(ga, gb)), f"{n(ga)} / {n(gb)}"),
                          ("Group to target", n(max(0, math.ceil(TARGETS[oem]['TOT'] * gb - ga))), "policies")])
                rep.table(gm[["Code", "Dealer", "RM", "Base", "Renewed", "Ret %", "RAG"]])
    if len(s_):
        x = s_.iloc[0]
        rep.h("Royal Enfield - current month")
        rep.kpis([("FY conversion", pct(ratio(x["fy_ach"], x["fy_base"])), f"{n(x['fy_ach'])} / {n(x['fy_base'])}"),
                  ("July baseline", pct(ratio(x["jul_ach"], x["jul_base"])), x["retention_band"]),
                  ("Incentive tier", f"+{int(x['tier'])} pts" if x["tier"] else "None", f"next needs {n(x['nop_to_next_tier'])} policies"),
                  ("AI conversions", n(x.get("ai_conversions", 0)), f"{n(x.get('ai_calls', 0))} calls")])
        from . import ra
        rep.kpis(ra.revenue_lines(x["fy_base"], x["fy_ach"], TARGETS["ROYAL ENFIELD"]["FY"], ra.RS_DEALER),
                 f"Dealer revenue at ₹{ra.RS_DEALER} per retained first-year policy (approximate)")
    pen = pack.get("pc_penetration_month")
    if not pen.empty and (pen["dealer_code"] == dc).any():
        p = pen[pen["dealer_code"] == dc].sort_values("month")
        p = p.assign(Month=[month_label(m) for m in p["month"]], **{"Penetration %": [ratio(a, b) for a, b in zip(p["new_nop"], p["retail"])]})
        rep.h("Penetration")
        rep.table(p[["Month", "retail", "new_nop", "Penetration %", "renewal_base", "renewed"]].rename(
            columns={"retail": "Retail", "new_nop": "New NOP", "renewal_base": "Renewal base", "renewed": "Renewed"}))
    for t_ in ("pc_payouts", "re_payouts"):
        pay = pack.get(t_)
        if not pay.empty:
            pp = pay[(pay["dealer_code"] == dc) & (~pay["paid"])]
            if len(pp):
                rep.h("Payouts pending")
                rep.table(pp.groupby("status", as_index=False).agg(Invoices=("base_value", "size"), Value=("base_value", "sum")))
    oc = pack.get("volvo_open_cases")
    if not oc.empty:
        o = oc[oc["dealer_code"].str.contains(dc, regex=False)]
        if len(o):
            rep.h("Volvo not-renewed cases")
            rep.table(o[["expiry_date", "model", "insurer", "segment", "policy_year", "total_premium", "claims"]].rename(
                columns={"expiry_date": "Expiry", "model": "Model", "insurer": "Insurer", "segment": "FY/OY",
                         "policy_year": "Policy year", "total_premium": "Premium", "claims": "Claims"}))
    if not rep.blocks:
        rep.p(f"No data found for dealer code {dc}.")
    return rep


def data_quality_report(pack) -> Report:
    rep = Report("Data quality and sources", _asof_line(pack))
    src = pd.DataFrame([{"Type": f.label, "File": f.name, "As of": f.asof, "Book": f.book} for f in pack.sources.values()])
    rep.table(src, "Files used")
    rep.table(pd.DataFrame(pack.notes).rename(columns={"severity": "Severity", "source": "Source", "note": "Note"}), "Checks")
    tb = pd.DataFrame([{"Dataset": k, "Rows": len(v), "Columns": len(v.columns)} for k, v in pack.tables.items()])
    rep.table(tb, "Datasets available to the agent")
    return rep


def _asof_line(pack, book=None) -> str:
    if book: return f"{book} data as of {pack.asof.get(book, '-')}"
    return " · ".join(f"{k} as of {v}" for k, v in pack.asof.items())
