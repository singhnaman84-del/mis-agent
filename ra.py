"""Retention-Analyst parity for every OEM.

Ports the private-car Retention Analyst (Audi / Skoda / VW / Volvo) and the RESP analyst (Royal Enfield) feature set
to the all-OEM MIS: one dealer book across OEMs, similar-base peers, consolidated dealer groups, action-style
insights, and the report set - zone executive page, underperformers, first vs other year, movers, Volvo page,
open cases, dealer groups, dealer meeting deck, penetration, payout status, dealer list, RM scorecards, data
corrections, boom-call pack and multi-dealer selection report. Every builder returns a `Report`, so each one renders
on screen and exports to HTML / PDF / PPT / CSV the same way.
"""
from __future__ import annotations

import math
import statistics

import pandas as pd

from . import analytics as A
from .charts import ChartSpec, from_df
from .model import OEM_LABEL, OEMS, TARGETS, re_zm
from .report import Report
from .util import month_label, ratio

# Dealer principals trading under several codes (user-confirmed in the Retention Analyst, 28-Sep-2026).
# Matching is a name fragment within a brand, so new codes join automatically.
GROUPS = [
    {"name": "Lally Motors", "oem": "VOLKSWAGEN", "match": "LALLY"},
    {"name": "Regent Garage", "oem": "AUDI", "match": "REGENT"},
    {"name": "PSB Auto Sales", "oem": "VOLKSWAGEN", "match": "PSB"},
    {"name": "BLC Autosales", "oem": "SKODA", "match": "BLC"},
    {"name": "Sidak Automobiles", "oem": "SKODA", "match": "SIDAK"},
    {"name": "Malwa Motor Sales", "oem": "SKODA", "match": "MALWA"},
    {"name": "Berk Autozone", "oem": "SKODA", "match": "BERK"},
    {"name": "United Wheels", "oem": "SKODA", "match": "UNITED"},
]
RS_DEALER, RS_MANAGER = 240, 24        # RE revenue per retained policy: dealer view vs RM/ZM view
DROP_PTS = 8                           # a fall of this many points month on month is a red flag


def _nan(x):
    return x is None or (isinstance(x, float) and math.isnan(x))


def label(o):
    return OEM_LABEL.get(o, o)


# --------------------------------------------------------------------------- #
# the dealer book: one row per dealer code, every OEM
# --------------------------------------------------------------------------- #
def dealer_book(pack, zm: str | None = None, oem: str | None = None) -> pd.DataFrame:
    """Latest closed month per dealer (RE: first year, month to date), with prior month, momentum, YTD, MTD,
    policies to target and RAG against the user-set criteria."""
    rows = []
    r = pack.get("retention")
    pc = r[r["book"] == "Private car"] if not r.empty else r
    if zm and not pc.empty: pc = pc[pc["zm"] == zm]
    if oem and oem != "ROYAL ENFIELD" and not pc.empty: pc = pc[pc["oem"] == oem]
    for o, d in (pc.groupby("oem") if not pc.empty and oem != "ROYAL ENFIELD" else []):
        last, prev, cur = A.months(d)
        if not last: continue
        t = TARGETS.get(o, TARGETS["SKODA"])
        gl = A.rollup(d[d["month"] == last], ["dealer_code", "dealer_name", "zm", "rm"]).set_index("dealer_code")
        gp = A.rollup(d[d["month"] == prev], ["dealer_code"]).set_index("dealer_code") if prev else pd.DataFrame()
        gc = A.rollup(d[d["month"] == cur], ["dealer_code"]).set_index("dealer_code") if cur else pd.DataFrame()
        gy = A.rollup(d[~d["is_mtd"].astype(bool)], ["dealer_code"]).set_index("dealer_code")
        for code, x in gl.iterrows():
            prior = gp["Ret %"].get(code) if not gp.empty else None
            rows.append({"Code": code, "Dealer": x["dealer_name"], "OEM": label(o), "oem": o, "ZM": x["zm"], "RM": x["rm"],
                         "Period": month_label(last), "Base": x["Base"], "Renewed": x["Renewed"], "Ret %": x["Ret %"],
                         "FY %": x["FY %"], "OY %": x["OY %"], "FY base": x["FY base"], "Prior %": prior,
                         "Change (pts)": None if _nan(prior) or _nan(x["Ret %"]) else (x["Ret %"] - prior) * 100,
                         "YTD %": gy["Ret %"].get(code), "MTD %": gc["Ret %"].get(code) if not gc.empty else None,
                         "Target %": t["TOT"],
                         "NOP to target": max(0, math.ceil(t["TOT"] * x["Base"] - x["Renewed"])) if x["Base"] else 0,
                         "RAG": A.rag(x["Ret %"], t["TOT"])})
    if oem in (None, "ROYAL ENFIELD"):
        s = pack.get("re_dealer_snapshot")
        if not s.empty:
            if zm: s = s[s["zm"] == zm]
            t = TARGETS["ROYAL ENFIELD"]
            for x in s.itertuples():
                ret = ratio(x.fy_ach, x.fy_base)
                # July first-year baseline (the incentive scheme's reference); only trusted on a real base
                prior = ratio(x.jul_ach, x.jul_base) if x.jul_base >= A.MIN_BASE and x.jul_ach <= x.jul_base else None
                rows.append({"Code": x.dealer_code, "Dealer": x.dealer_name, "OEM": "Royal Enfield", "oem": "ROYAL ENFIELD",
                             "ZM": x.zm, "RM": x.rm, "Period": "MTD (first year)", "Base": x.fy_base, "Renewed": x.fy_ach,
                             "Ret %": ret, "FY %": ret, "OY %": None, "FY base": x.fy_base, "Prior %": prior,
                             "Change (pts)": None if _nan(prior) or _nan(ret) or ret > 1 else (ret - prior) * 100,
                             "YTD %": None, "MTD %": ret, "Target %": t["FY"],
                             "NOP to target": max(0, math.ceil(t["FY"] * x.fy_base - x.fy_ach)) if x.fy_base else 0,
                             "RAG": A.rag(ret, t["FY"])})
    df = pd.DataFrame(rows)
    if df.empty: return df
    df["Flag"] = df["Base"] >= A.MIN_BASE
    return df


# --------------------------------------------------------------------------- #
# peers and groups
# --------------------------------------------------------------------------- #
def peers(book: pd.DataFrame, code: str) -> dict:
    """Similar-base peer band: same OEM and zone, base within ±30%, widening to ±50% then the whole zone+brand."""
    me = book[book["Code"] == code]
    if me.empty: return {}
    m = me.iloc[0]
    pool = book[(book["oem"] == m["oem"]) & (book["ZM"] == m["ZM"]) & (book["Code"] != code) & (book["Base"] > 0)]
    sel, band = pool, "whole zone + brand"
    for bd, lab in ((0.30, "±30%"), (0.50, "±50%")):
        s = pool[(pool["Base"] - m["Base"]).abs() <= bd * max(m["Base"], 1)]
        if len(s) >= 3:
            sel, band = s, lab
            break
    vals = sel.dropna(subset=["Ret %"]).sort_values("Ret %", ascending=False)
    if vals.empty: return {"band": band, "n": 0}
    rank = 1 + int((vals["Ret %"] > m["Ret %"]).sum()) if not _nan(m["Ret %"]) else None
    best = vals.iloc[0]
    return {"band": band, "n": len(vals), "median": statistics.median(vals["Ret %"]), "best_name": best["Dealer"],
            "best_code": best["Code"], "best": best["Ret %"], "best_base": best["Base"], "rank": rank, "list": vals}


def group_of(row) -> dict | None:
    for g in GROUPS:
        if row["oem"] == g["oem"] and g["match"] in str(row["Dealer"]).upper():
            return g
    return None


def group_members(book, g) -> pd.DataFrame:
    return book[(book["oem"] == g["oem"]) & book["Dealer"].astype(str).str.upper().str.contains(g["match"], regex=False)]


# --------------------------------------------------------------------------- #
# insights with actions
# --------------------------------------------------------------------------- #
def action_insights(book: pd.DataFrame, pack=None) -> list[str]:
    out = []
    ds = book[book["Flag"]] if not book.empty else book
    if ds.empty: return [f"No dealers with base ≥ {A.MIN_BASE}."]
    for i, (_, d) in enumerate(ds.sort_values("NOP to target", ascending=False).head(3).iterrows()):
        if d["NOP to target"] <= 0: continue
        pr = peers(book, d["Code"])
        proof = (f" Peer proof: {pr['best_name']} at {A.pct(pr['best'])} on a similar base."
                 if pr.get("best") is not None and pr["best"] >= d["Target %"] else "")
        out.append(f"**{d['Dealer']}** ({d['OEM']}, RM {str(d['RM']).title()}): {A.n(d['Renewed'])}/{A.n(d['Base'])} = "
                   f"{A.pct(d['Ret %'])} in {d['Period']}, {A.n(d['NOP to target'])} policies short of the "
                   f"{A.pct(d['Target %'])} target - the {['largest', '2nd-largest', '3rd-largest'][i]} shortfall.{proof} "
                   f"Action: RM to review the {A.n(d['Base'] - d['Renewed'])} unrenewed policies with the dealer this week.")
    drops = ds.dropna(subset=["Change (pts)"]).sort_values("Change (pts)").head(2)
    for _, d in drops.iterrows():
        if d["Change (pts)"] <= -DROP_PTS:
            out.append(f"**{d['Dealer']}** ({d['OEM']}) fell {abs(d['Change (pts)']):.1f} pts to {A.pct(d['Ret %'])} "
                       f"(base {A.n(d['Base'])}). Action: RM {str(d['RM']).title()} to find what changed - telecaller, "
                       "insurer quotes or data upload.")
    for o, bd in ds[ds["oem"] != "ROYAL ENFIELD"].groupby("oem"):
        fb, fa = bd["FY base"].sum(), (bd["FY %"].fillna(0) * bd["FY base"]).sum()
        ob = bd["Base"].sum() - fb
        oa = bd["Renewed"].sum() - fa
        fp, op = ratio(fa, fb), ratio(oa, ob)
        if not _nan(fp) and not _nan(op) and op - fp >= 0.10:
            out.append(f"{label(o)}: first-year customers renew at {A.pct(fp)} vs other-year {A.pct(op)}. First-time "
                       "customers churn fastest - a first-renewal call plan is the most actionable lever.")
    for (o, rm), rd in ds.groupby(["oem", "RM"]):
        if len(rd) >= 2 and (rd["RAG"] == "Red").all():
            out.append(f"RM **{str(rm).title()}** ({label(o)}): every dealer in the book (n={len(rd)}) is Red. "
                       "Escalate in the RM review.")
    return out[:8] or ["Every flagged dealer is within the amber band of target."]


# --------------------------------------------------------------------------- #
# report builders
# --------------------------------------------------------------------------- #
def _scope(zm): return f"Zone {zm.title()}" if zm else "All zones"


def _tbl(df, cols):
    return df[[c for c in cols if c in df.columns]]


DEALER_COLS = ["Code", "Dealer", "OEM", "RM", "ZM", "Period", "Base", "Renewed", "Ret %", "FY %", "OY %", "Change (pts)",
               "NOP to target", "RAG"]


def zone_exec(pack, zm=None) -> Report:
    book = dealer_book(pack, zm)
    rep = Report(f"Zone executive page - {_scope(zm)}", A._asof_line(pack))
    tiles = []
    for o in OEMS:
        b = book[book["oem"] == o]
        if b.empty: continue
        p = ratio(b["Renewed"].sum(), b["Base"].sum())
        tiles.append((f"{label(o)} · {b['Period'].iloc[0]}", A.pct(p),
                      f"{A.n(b['Renewed'].sum())}/{A.n(b['Base'].sum())} · target {A.pct(b['Target %'].iloc[0])} · "
                      f"{int((b['RAG'] == 'Red').sum())} Red"))
    rep.kpis(tiles, "By OEM")
    rep.h("What needs action"); rep.bullets(action_insights(book, pack))
    worst = book[book["Flag"]].sort_values("NOP to target", ascending=False).head(15)
    rep.chart(from_df(worst, "Dealer", "NOP to target", "hbar", "Dealers furthest below target (policies short)"))
    rep.table(_tbl(worst, DEALER_COLS), "Dealers furthest below target")
    rms = rm_table(book)
    rep.h("RMs"); rep.table(rms, "RM standing")
    return rep


def rm_table(book) -> pd.DataFrame:
    if book.empty: return book
    g = book.groupby(["ZM", "RM", "OEM"], as_index=False).agg(Dealers=("Code", "size"), Base=("Base", "sum"),
                                                              Renewed=("Renewed", "sum"), Red=("RAG", lambda x: int((x == "Red").sum())),
                                                              **{"NOP to target": ("NOP to target", "sum")})
    g["Ret %"] = [ratio(a, b) for a, b in zip(g["Renewed"], g["Base"])]
    return g.sort_values(["Red", "NOP to target"], ascending=False)[["ZM", "RM", "OEM", "Dealers", "Base", "Renewed", "Ret %", "Red",
                                                                     "NOP to target"]]


def underperformers(pack, zm=None) -> Report:
    book = dealer_book(pack, zm)
    rep = Report(f"Underperformers - {_scope(zm)}", "Red dealers with policies to target and a similar-base peer already at target")
    red = book[book["Flag"] & (book["RAG"] == "Red")].sort_values("NOP to target", ascending=False)
    rows = []
    for _, d in red.iterrows():
        pr = peers(book, d["Code"])
        rows.append({**{c: d[c] for c in DEALER_COLS if c in d}, "Peer at target": pr.get("best_name") if pr.get("best", 0) and pr["best"] >= d["Target %"] else "",
                     "Peer %": pr.get("best") if pr.get("best", 0) and pr["best"] >= d["Target %"] else None,
                     "Peer band": pr.get("band", "")})
    t = pd.DataFrame(rows)
    rep.kpis([("Red dealers", A.n(len(red)), f"base ≥ {A.MIN_BASE}"), ("Policies to target", A.n(red["NOP to target"].sum()), "if every Red dealer hit target"),
              (f"Dropped ≥ {DROP_PTS} pts", A.n((book["Change (pts)"] <= -DROP_PTS).sum()), "month on month")])
    rep.table(t, "Red dealers, worst gap first")
    drops = book[book["Change (pts)"] <= -DROP_PTS].sort_values("Change (pts)")
    rep.table(_tbl(drops, DEALER_COLS), f"Fell {DROP_PTS}+ points month on month")
    return rep


def fy_vs_oy(pack, zm=None) -> Report:
    rep = Report(f"First year vs other year - {_scope(zm)}", "Where the book leaks: first-time renewals vs repeat renewals")
    rows = []
    for o in OEMS:
        if o == "ROYAL ENFIELD":
            rg = pack.get("re_region")
            if rg.empty: continue
            if zm: rg = rg[[re_zm(x) == zm for x in rg["region"]]]
            fb, fa, ob, oa = rg["fy_base"].sum(), rg["fy_ach"].sum(), rg["oy_base"].sum(), rg["oy_ach"].sum()
            per = "MTD"
        else:
            d = A.oem_frame(pack, o, zm)
            if d.empty: continue
            last, _, _ = A.months(d)
            x = A.rollup(d[d["month"] == last], ["oem"])
            if x.empty: continue
            x = x.iloc[0]; fb, fa, ob, oa = x["FY base"], x["FY ach"], x["OY base"], x["OY ach"]; per = month_label(last)
        rows.append({"OEM": label(o), "Period": per, "FY base": fb, "FY renewed": fa, "FY %": ratio(fa, fb), "OY base": ob,
                     "OY renewed": oa, "OY %": ratio(oa, ob), "Gap (pts)": (ratio(oa, ob) - ratio(fa, fb)) * 100,
                     "FY target %": TARGETS[o]["FY"], "FY share of base": ratio(fb, fb + ob)})
    t = pd.DataFrame(rows)
    if len(t):
        rep.chart(ChartSpec("bar", list(t["OEM"]), {"First year": list(t["FY %"]), "Other year": list(t["OY %"])},
                            "First year vs other year retention by OEM", pct=True))
        rep.table(t, "By OEM")
        lead = t.sort_values("Gap (pts)", ascending=False).iloc[0]
        rep.bullets([f"The widest leak is {lead['OEM']}: first-year {A.pct(lead['FY %'])} vs other-year {A.pct(lead['OY %'])} "
                     f"({lead['Gap (pts)']:.0f} pts), with first-year policies {A.pct(lead['FY share of base'])} of the base.",
                     "First-year customers have never renewed with the dealer before - call them 30-45 days before expiry, "
                     "not in the last week."])
    book = dealer_book(pack, zm)
    fyd = book[book["Flag"] & (book["oem"] != "ROYAL ENFIELD")].copy()
    if len(fyd):
        fyd["FY gap (pts)"] = (fyd["OY %"] - fyd["FY %"]) * 100
        rep.table(_tbl(fyd.sort_values("FY gap (pts)", ascending=False).head(20),
                       ["Code", "Dealer", "OEM", "RM", "FY base", "FY %", "OY %", "FY gap (pts)", "Ret %"]), "Dealers with the widest first-year gap")
    return rep


def movers(pack, zm=None) -> Report:
    book = dealer_book(pack, zm)
    rep = Report(f"Movers - {_scope(zm)}", "Biggest improvers and decliners against the prior month (base ≥ minimum)")
    m = book[book["Flag"]].dropna(subset=["Change (pts)"])
    rep.table(_tbl(m.sort_values("Change (pts)").head(15), DEALER_COLS + ["Prior %"]), "Biggest decliners")
    rep.table(_tbl(m.sort_values("Change (pts)", ascending=False).head(15), DEALER_COLS + ["Prior %"]), "Biggest improvers")
    return rep


def volvo_page(pack, zm=None) -> Report:
    rep = Report(f"Volvo page - {_scope(zm)}", "Retention by month, first vs other year, EV vs non-EV, dealer detail and open cases")
    rep.extend(A.oem_report(pack, "VOLVO", zm))
    ve = pack.get("volvo_expiry_month")
    if not ve.empty:
        asof = pack.asof.get("Private car", "9999-12")[:7]
        v = ve[(ve["month"] <= asof) & ve["ev"].isin(["EV", "Non EV"])]
        g = v.groupby(["month", "ev"], as_index=False)[["base", "ach"]].sum()
        g["ret"] = [ratio(a, b) for a, b in zip(g["ach"], g["base"])]
        pv = g.pivot(index="month", columns="ev", values="ret").sort_index()
        rep.h("EV vs non-EV")
        rep.chart(ChartSpec("line", [month_label(x) for x in pv.index], {c: list(pv[c]) for c in pv.columns},
                            "Volvo retention - EV vs non-EV", pct=True))
    return rep


def groups_report(pack, zm=None) -> Report:
    book = dealer_book(pack, zm)
    rep = Report(f"Consolidated dealer groups - {_scope(zm)}", "Named groups combined across their dealer codes, with each code listed")
    rows = []
    for g in GROUPS:
        m = group_members(book, g)
        if m.empty: continue
        b, a = m["Base"].sum(), m["Renewed"].sum()
        t = TARGETS[g["oem"]]["TOT"]
        rows.append({"Group": g["name"], "OEM": label(g["oem"]), "Codes": len(m), "Base": b, "Renewed": a, "Ret %": ratio(a, b),
                     "NOP to target": max(0, math.ceil(t * b - a)), "RAG": A.rag(ratio(a, b), t)})
        rep.table(_tbl(m, ["Code", "Dealer", "RM", "Base", "Renewed", "Ret %", "RAG"]), f"{g['name']} - {len(m)} codes")
    if rows:
        rep.blocks.insert(0, Report("").table(pd.DataFrame(rows), "Groups").blocks[0])
    else:
        rep.p("None of the named groups has dealers in this scope.")
    return rep


def meeting_deck(pack, code) -> Report:
    """Dealer-facing review: aggregate only, anonymised peers, no RM visit data, no customer rows, ends on actions."""
    book = dealer_book(pack)
    me = book[book["Code"] == code]
    if me.empty: return Report("Dealer meeting deck", "").p(f"No dealer {code} in the book.")
    d = me.iloc[0]
    rep = Report(f"{d['Dealer']} - renewal review", f"{d['OEM']} · {code} · prepared for the dealer meeting")
    rep.kpis([(f"Overall {d['Period']}", A.pct(d["Ret %"]), f"{A.n(d['Renewed'])} of {A.n(d['Base'])} renewed · target {A.pct(d['Target %'])}"),
              ("First year", A.pct(d["FY %"]), f"target {A.pct(TARGETS[d['oem']]['FY'])}"),
              ("Other year", A.pct(d["OY %"]), f"target {A.pct(TARGETS[d['oem']]['OY'])}"),
              ("To target", A.n(d["NOP to target"]), "extra renewals to reach target")])
    if d["oem"] != "ROYAL ENFIELD":
        r = pack.get("retention"); x = r[r["dealer_code"] == code]
        m = A.rollup(x, ["month"]).sort_values("month")
        _, _, cur = A.months(x)
        m["Month"] = [month_label(v) + (" (to date)" if v == cur else "") for v in m["month"]]
        rep.h("Where we are, month by month")
        rep.chart(ChartSpec("line", list(m["Month"]), {"Overall": list(m["Ret %"]), "First year": list(m["FY %"]),
                                                       "Other year": list(m["OY %"])}, "Retention by month", pct=True,
                            target=d["Target %"]))
        rep.table(m[["Month", "Base", "Renewed", "Ret %", "FY %", "OY %"]])
    pr = peers(book, code)
    if pr.get("n"):
        rep.h("How this compares with similar dealers")
        rep.table(pd.DataFrame([{"Measure": "Retention", "This dealership": d["Ret %"], "Peer median": pr["median"],
                                 "Best in band": pr["best"], "Rank": f"{pr['rank']} of {pr['n'] + 1}", "Peer band": pr["band"]}]))
        rep.bullets([f"{pr['n']} dealers of the same brand in the zone with a similar renewal base (anonymised)."])
    pen = pack.get("pc_penetration_month")
    if not pen.empty and (pen["dealer_code"] == code).any():
        p = pen[pen["dealer_code"] == code].sort_values("month").tail(6)
        p = p.assign(Month=[month_label(v) for v in p["month"]], **{"Penetration %": [ratio(a, b) for a, b in zip(p["new_nop"], p["retail"])]})
        rep.h("New business - policies attached to cars sold")
        rep.table(p[["Month", "retail", "new_nop", "Penetration %"]].rename(columns={"retail": "Cars sold", "new_nop": "Policies"}))
    oc = pack.get("volvo_open_cases")
    if not oc.empty:
        o = oc[oc["dealer_code"].str.contains(code, regex=False)]
        if len(o):
            rep.h("Open this month - expired, not yet renewed")
            rep.kpis([("Policies", A.n(len(o)), ""), ("Premium", A.inr(o["total_premium"].sum()), ""),
                      ("First year", A.n((o["segment"] == "FY").sum()), "")])
    rep.h("Agreed actions")
    rep.table(pd.DataFrame([{"#": i, "Action": "", "Owner": "", "By when": ""} for i in range(1, 6)]))
    return rep


def dealer_list(pack, zm=None, oem=None, rag_filter=None, rm=None, text=None) -> Report:
    book = dealer_book(pack, zm, oem)
    if rm: book = book[book["RM"] == rm]
    if rag_filter: book = book[book["RAG"].isin(rag_filter)]
    if text: book = book[(book["Dealer"].astype(str) + " " + book["Code"].astype(str)).str.contains(text, case=False, regex=False)]
    rep = Report(f"Dealer list - {_scope(zm)}", f"{len(book)} dealers on the current filters, worst gap first")
    rep.table(_tbl(book.sort_values(["Flag", "NOP to target"], ascending=[False, False]), DEALER_COLS + ["YTD %", "MTD %"]))
    return rep


def rm_scorecards(pack, zm=None) -> Report:
    book = dealer_book(pack, zm)
    rep = Report(f"RM scorecards - {_scope(zm)}", "Each RM's whole book with policies to target")
    rep.table(rm_table(book), "All RMs")
    for (rm, o), rd in book.groupby(["RM", "OEM"]):
        if not rm: continue
        b, a = rd["Base"].sum(), rd["Renewed"].sum()
        rep.h(f"{str(rm).title()} - {o}")
        rep.kpis([("Retention", A.pct(ratio(a, b)), f"{A.n(a)}/{A.n(b)}"), ("Dealers", A.n(len(rd)), f"{int((rd['RAG'] == 'Red').sum())} Red"),
                  ("Policies to target", A.n(rd["NOP to target"].sum()), "")])
        rep.table(_tbl(rd.sort_values("NOP to target", ascending=False).head(10), ["Code", "Dealer", "Base", "Renewed", "Ret %", "Change (pts)",
                                                                                  "NOP to target", "RAG"]))
    return rep


def penetration_report(pack, zm=None) -> Report:
    rep = Report(f"Penetration - {_scope(zm)}", "Policies attached to vehicles sold, by month and by dealer")
    rep.extend(A.penetration_section(pack, "ALL", zm))
    pen = pack.get("re_penetration")
    if not pen.empty:
        p = pen if not zm else pen[[re_zm(x) == zm for x in pen["region"]]]
        rep.h("Royal Enfield")
        rep.kpis([("Penetration MTD", A.pct(ratio(p["prog_mtd"].sum(), p["veh_mtd"].sum())), f"{A.n(p['prog_mtd'].sum())} / {A.n(p['veh_mtd'].sum())} bikes"),
                  ("Penetration YTD", A.pct(ratio(p["prog_ytd"].sum(), p["veh_ytd"].sum())), "")])
        low = p[p["veh_mtd"] >= 20].assign(**{"Penetration %": lambda x: x["prog_mtd"] / x["veh_mtd"]}).sort_values("Penetration %").head(15)
        rep.table(low[["dealer_code", "dealer_name", "rm", "veh_mtd", "prog_mtd", "Penetration %"]].rename(
            columns={"dealer_code": "Code", "dealer_name": "Dealer", "rm": "RM", "veh_mtd": "Bikes sold MTD", "prog_mtd": "Policies MTD"}),
            "Lowest penetration (20+ bikes sold this month)")
    return rep


def payout_report(pack, zm=None) -> Report:
    rep = Report(f"Payout status - {_scope(zm)}", "Pending invoices, who has to act, and what is already paid")
    for t, name in (("pc_payouts", "Private car"), ("re_payouts", "Royal Enfield")):
        sub = A.payout_section(pack, t, None, zm)
        if sub.blocks:
            rep.h(name); rep.extend(Report("", "", sub.blocks[1:]))
    owner = {"Invoice Not uploaded on Portal": "Dealer - upload the invoice on the portal",
             "Awaiting Physical invoice": "Dealer - send the signed physical invoice",
             "Discrepancy": "Dealer - re-upload with correct details", "HOLD for GSTIN": "Dealer - fix GSTIN status",
             "Bank details": "Dealer - correct bank details", "Payout on": "Finance - scheduled, no action"}
    rep.h("Who unblocks what")
    rep.table(pd.DataFrame([{"Status contains": k, "Owner / next step": v} for k, v in owner.items()]))
    return rep


def data_corrections(pack) -> Report:
    rep = Report("Data corrections applied", A._asof_line(pack))
    rep.bullets([n["note"] for n in pack.notes])
    rep.h("Rules")
    rep.bullets(["Retention % = renewed ÷ base, summed first - never an average of percentages.",
                 "Flags use the last closed month; the running month is shown as month to date.",
                 f"RAG: Green at or above target, Amber within {A.AMBER * 100:.0f} pts below, Red below that; "
                 f"dealers with base below {A.MIN_BASE} are not flagged.",
                 "Audi / Skoda / VW RM and ZM come from the snapshot sheet (the brand tabs label them the wrong way round).",
                 "Hidden grand-total rows are removed before any sum.",
                 "Customer identifiers are never loaded."])
    return rep


def boom_call(pack, zm) -> Report:
    """Daily stand-up pack for one ZM: scorecard, per-RM talking points, red flags, wins, Teams note."""
    book = dealer_book(pack, zm)
    rep = Report(f"Boom call - {str(zm).title()}", A._asof_line(pack))
    if book.empty: return rep.p("No dealers in this zone.")
    sc = []
    for o in OEMS:
        b = book[book["oem"] == o]
        if b.empty: continue
        sc.append((label(o), A.pct(ratio(b["Renewed"].sum(), b["Base"].sum())),
                   f"{b['Period'].iloc[0]} · target {A.pct(b['Target %'].iloc[0])} · {A.n(b['NOP to target'].sum())} short"))
    rep.kpis(sc, "Scorecard")
    rep.h("Talking points by RM")
    note_lines = [f"*Boom call - {str(zm).title()}*"] + [f"• {k[0]}: {k[1]} ({k[2]})" for k in sc] + [""]
    for rm, rd in book[book["Flag"]].groupby("RM"):
        if not rm: continue
        w = rd.sort_values("NOP to target", ascending=False).iloc[0]
        b, a = rd["Base"].sum(), rd["Renewed"].sum()
        pts_ = [f"Book at {A.pct(ratio(a, b))} across {len(rd)} dealers; {int((rd['RAG'] == 'Red').sum())} Red, "
                f"{A.n(rd['NOP to target'].sum())} policies to target."]
        if w["NOP to target"] > 0:
            pts_.append(f"Biggest gap: {w['Dealer']} ({w['OEM']}) - {A.n(w['NOP to target'])} short at {A.pct(w['Ret %'])}. Ask: plan for the "
                        f"{A.n(w['Base'] - w['Renewed'])} unrenewed policies by Friday.")
        dr = rd.dropna(subset=["Change (pts)"]).sort_values("Change (pts)")
        if len(dr) and dr.iloc[0]["Change (pts)"] <= -DROP_PTS:
            pts_.append(f"Watch: {dr.iloc[0]['Dealer']} down {abs(dr.iloc[0]['Change (pts)']):.0f} pts.")
        rep.bullets(pts_, str(rm).title())
        note_lines.append(f"*{str(rm).title()}* - " + " ".join(pts_))
    flags = book[book["Flag"] & ((book["Change (pts)"] <= -DROP_PTS) | ((book["Renewed"] == 0) & (book["Base"] >= A.MIN_BASE)))]
    rep.h("Red flags")
    rep.bullets([f"{r['Dealer']} ({r['OEM']}, RM {str(r['RM']).title()}): " +
                 ("zero renewals on a base of " + A.n(r["Base"]) if r["Renewed"] == 0 else f"down {abs(r['Change (pts)']):.0f} pts to {A.pct(r['Ret %'])}")
                 for _, r in flags.head(10).iterrows()] or ["None today."])
    wins = book[book["Flag"] & ((book["RAG"] == "Green") | (book["Change (pts)"] >= DROP_PTS))].sort_values("Change (pts)", ascending=False)
    rep.h("Wins")
    rep.bullets([f"{r['Dealer']} ({r['OEM']}): {A.pct(r['Ret %'])}" + (f", up {r['Change (pts)']:.0f} pts" if not _nan(r["Change (pts)"]) and r["Change (pts)"] > 0 else "")
                 for _, r in wins.head(8).iterrows()] or ["None today."])
    rep.h("Teams note (copy and paste)")
    rep.p("\n".join(note_lines))
    return rep


def selection_report(pack, codes: list[str]) -> Report:
    """Several ticked dealers: each code in full, then a side-by-side summary."""
    book = dealer_book(pack)
    sel = book[book["Code"].isin(codes)]
    rep = Report(f"Selected dealers ({len(codes)})", "Each code's dashboard, then a summary of them side by side")
    rep.table(_tbl(sel, DEALER_COLS + ["YTD %", "MTD %"]), "Summary")
    for c in codes:
        sub = A.dealer_report(pack, c)
        rep.h(sub.title); rep.extend(sub)
    return rep


def revenue_lines(fy_base, fy_ach, target, rate) -> list[tuple]:
    earned = fy_ach * rate
    at_t = math.ceil(fy_base * target) * rate
    rem = at_t - earned
    return [("Revenue earned", A.inr(earned), f"{A.n(fy_ach)} × ₹{rate}"), ("At target", A.inr(at_t), f"{A.pct(target)} of base"),
            ("Remaining", A.inr(rem) if rem > 0 else "—", "blank once at target")]


REPORTS = [
    ("zone", "Zone executive page", "KPIs by OEM, dealers furthest below target, and the actions that follow."),
    ("boom", "Boom call pack", "Daily stand-up pack for a ZM: scorecard, per-RM talking points, red flags, wins and a Teams note."),
    ("under", "Underperformers", "Red dealers with policies-to-target and a similar-base peer already at target."),
    ("fy", "First vs other year", "Where the book leaks, by OEM and by dealer."),
    ("movers", "Movers", "Biggest improvers and decliners against the prior month."),
    ("volvo", "Volvo page", "Retention by month, first vs other year, EV vs non-EV, dealer detail and open cases."),
    ("re", "Royal Enfield page", "First-year conversion, AI calling, incentive, focus dealers, visits, penetration, payouts."),
    ("open", "Open cases", "Expired and not renewed - the chase list, aggregated."),
    ("groups", "Consolidated dealer groups", "Named groups combined across their dealer codes, with each code listed."),
    ("meeting", "Dealer meeting deck", "Dealer-facing review: performance, anonymised peers, new business, open cases and agreed actions."),
    ("penetration", "Penetration", "Policies attached to vehicles sold, by month and by dealer."),
    ("payout", "Payout status", "Pending invoices, who has to act, and what is already paid."),
    ("dealers", "Dealer list", "Every dealer on the current filters, worst first."),
    ("rms", "RM scorecards", "Each RM's whole book with policies to target."),
    ("rm", "Full RM review", "One RM's whole book across OEMs - every parameter."),
    ("dealer", "Full dealer review", "One dealer - trend, peers, penetration, open cases, payouts."),
    ("quality", "Data corrections", "What is corrected on read, and the rules behind every figure."),
]


def build(pack, rid, zm=None, who=None) -> Report:
    if rid == "zone": return zone_exec(pack, zm)
    if rid == "boom":
        return boom_call(pack, zm) if zm else Report("Boom call pack").p("Pick a zone (ZM) - the boom call is one ZM's stand-up pack.")
    if rid == "under": return underperformers(pack, zm)
    if rid == "fy": return fy_vs_oy(pack, zm)
    if rid == "movers": return movers(pack, zm)
    if rid == "volvo": return volvo_page(pack, zm)
    if rid == "re": return A.re_report(pack, zm)
    if rid == "open":
        r = Report(f"Open cases - {_scope(zm)}", "Volvo policies expired and not renewed (no customer identifiers)")
        return r.extend(A.volvo_open_section(pack, zm))
    if rid == "groups": return groups_report(pack, zm)
    if rid == "meeting": return meeting_deck(pack, who) if who else Report("Dealer meeting deck").p("Pick a dealer.")
    if rid == "penetration": return penetration_report(pack, zm)
    if rid == "payout": return payout_report(pack, zm)
    if rid == "dealers": return dealer_list(pack, zm)
    if rid == "rms": return rm_scorecards(pack, zm)
    if rid == "quality": return data_corrections(pack)
    raise ValueError(rid)
