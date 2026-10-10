"""OEM pages for the app: YTD / MTD / last-month views, defaulter RMs, and the Power BI toolkit.

Pages: Overall, Audi, Royal Enfield, Skoda, Volkswagen, Volvo. Every builder returns a `Report`, so it renders and
exports like the rest of the app.

Periods
- Private cars: YTD = calendar year to the MIS date (Jan .. running month, running month included), MTD = the running
  month, LM = the last closed month.
- Royal Enfield: MTD is national (region view of the renewal report). YTD is financial-year to date (Apr ..) and only
  exists at dealer level for the North zone (monthly history file) - it is labelled that way wherever it is shown.
"""
from __future__ import annotations

import io
import json
import math
import re
import zipfile

import pandas as pd

from . import analytics as A
from .charts import ChartSpec, from_df
from .model import OEM_LABEL, TARGETS, re_zm
from .report import Report
from .util import month_label, ratio

PAGES = ["OVERALL", "AUDI", "ROYAL ENFIELD", "SKODA", "VOLKSWAGEN", "VOLVO"]
PAGE_LABEL = {"OVERALL": "Overall", **OEM_LABEL}
CAR_OEMS = ["AUDI", "SKODA", "VOLKSWAGEN", "VOLVO"]
SEGS = ("FY", "OY")


# --------------------------------------------------------------------------- #
# period arithmetic
# --------------------------------------------------------------------------- #
def _agg(d: pd.DataFrame) -> dict:
    out = {}
    for seg in SEGS:
        x = d[d["segment"] == seg] if not d.empty else d
        b, a = (float(x["base"].sum()), float(x["ach"].sum())) if not x.empty else (0.0, 0.0)
        out[f"{seg} base"], out[f"{seg} ach"], out[f"{seg} %"] = b, a, ratio(a, b)
    out["Base"] = out["FY base"] + out["OY base"]
    out["Renewed"] = out["FY ach"] + out["OY ach"]
    out["Ret %"] = ratio(out["Renewed"], out["Base"])
    return out


def _empty():
    return _agg(pd.DataFrame(columns=["segment", "base", "ach"]))


def period_frames(d: pd.DataFrame) -> dict:
    """Split a retention slice into YTD / MTD / LM frames with labels."""
    if d.empty:
        return {"ytd": d, "mtd": d, "lm": d, "ytd_label": "YTD", "mtd_label": "MTD", "lm_label": "Last month"}
    last, _, cur = A.months(d)
    top = cur or last
    book = d["book"].iloc[0] if "book" in d else ""
    if book == "Royal Enfield":     # financial year: April onwards
        y, m = int(top[:4]), int(top[5:7])
        start = f"{y if m >= 4 else y - 1:04d}-04"
    else:
        start = f"{top[:4]}-01"
    ytd = d[(d["month"] >= start) & (d["month"] <= top)]
    return {"ytd": ytd, "mtd": d[d["month"] == cur] if cur else d.iloc[0:0], "lm": d[d["month"] == last] if last else d.iloc[0:0],
            "ytd_label": f"YTD {month_label(start)}-{month_label(top)}", "mtd_label": f"MTD {month_label(cur)}" if cur else "MTD",
            "lm_label": month_label(last) if last else "Last month"}


def re_mtd(pack, zm=None, rm=None) -> dict:
    """RE national month to date: FY/OY from the region view (zone-filterable); RM scope from the dealer snapshot (FY)."""
    if rm:
        s = pack.get("re_dealer_snapshot")
        s = s[s["rm"] == rm] if not s.empty else s
        out = _empty()
        if not s.empty:
            out.update({"FY base": s["fy_base"].sum(), "FY ach": s["fy_ach"].sum()})
            out["FY %"] = ratio(out["FY ach"], out["FY base"])
            out["Base"], out["Renewed"], out["Ret %"] = out["FY base"], out["FY ach"], out["FY %"]
        return out
    rg = pack.get("re_region")
    if rg.empty: return _empty()
    if zm: rg = rg[[re_zm(x) == zm for x in rg["region"]]]
    out = {"FY base": rg["fy_base"].sum(), "FY ach": rg["fy_ach"].sum(), "OY base": rg["oy_base"].sum(), "OY ach": rg["oy_ach"].sum()}
    out["FY %"], out["OY %"] = ratio(out["FY ach"], out["FY base"]), ratio(out["OY ach"], out["OY base"])
    out["Base"], out["Renewed"] = out["FY base"] + out["OY base"], out["FY ach"] + out["OY ach"]
    out["Ret %"] = ratio(out["Renewed"], out["Base"])
    out["FY prev %"] = ratio(rg["fy_ach_prev"].sum(), rg["fy_base_prev"].sum())
    return out


def summary(pack, oem: str, zm=None, rm=None) -> dict:
    """{'ytd','mtd','lm': metrics, labels, 'target', 'note'} for one OEM (or 'PC' = all private-car brands)."""
    d = A.oem_frame(pack, "ALL" if oem == "PC" else oem, zm, rm)
    if oem == "PC": d = d[d["oem"].isin(CAR_OEMS)]
    f = period_frames(d)
    out = {"ytd": _agg(f["ytd"]), "mtd": _agg(f["mtd"]), "lm": _agg(f["lm"]), "ytd_label": f["ytd_label"],
           "mtd_label": f["mtd_label"], "lm_label": f["lm_label"], "target": TARGETS.get(oem, TARGETS["SKODA"]), "note": ""}
    if oem == "ROYAL ENFIELD":
        out["mtd"] = re_mtd(pack, zm, rm)
        out["mtd_label"] = "MTD " + (month_label(pack.asof.get("Royal Enfield", "")[:7]) or "")
        out["ytd_label"] = out["ytd_label"].replace("YTD", "FYTD") + " (North)"
        out["note"] = "RE month to date is national; RE year to date is the North-zone dealer history only."
    return out


def _has(m) -> bool:
    return bool(m and m.get("Base"))


# --------------------------------------------------------------------------- #
# group tables: YTD / LM / MTD side by side
# --------------------------------------------------------------------------- #
def ytd_mtd_table(pack, oem: str, by: list[str], zm=None, rm=None) -> pd.DataFrame:
    """One row per group with YTD base/renewed/%, last-month %, MTD %, FY/OY YTD %, gap and RAG on YTD."""
    d = A.oem_frame(pack, oem, zm, rm)
    if d.empty: return pd.DataFrame()
    f = period_frames(d)
    t = TARGETS.get(oem, TARGETS["SKODA"])
    y = A.rollup(f["ytd"], by).set_index(by)
    lm = A.rollup(f["lm"], by).set_index(by)["Ret %"] if not f["lm"].empty else pd.Series(dtype=float)
    mt = A.rollup(f["mtd"], by).set_index(by)["Ret %"] if not f["mtd"].empty else pd.Series(dtype=float)
    out = pd.DataFrame({"YTD base": y["Base"], "YTD renewed": y["Renewed"], "YTD %": y["Ret %"],
                        "FY YTD %": y["FY %"], "OY YTD %": y["OY %"]})
    out[f"{f['lm_label']} %"] = lm.reindex(out.index)
    out[f"{f['mtd_label']} %"] = mt.reindex(out.index)
    out["Target %"] = t["TOT"]
    out["Gap (pts)"] = (out["YTD %"] - t["TOT"]) * 100
    out["NOP to target"] = [max(0, math.ceil(t["TOT"] * b - a)) if b else 0 for b, a in zip(out["YTD base"], out["YTD renewed"])]
    out["RAG"] = [A.rag(v, t["TOT"]) for v in out["YTD %"]]
    return out.reset_index()


def re_group_table(pack, by: str, zm=None) -> pd.DataFrame:
    """RE by RM / ZM / dealer: national MTD first-year (snapshot) + North FYTD from the history."""
    s = pack.get("re_dealer_snapshot")
    if s.empty: return pd.DataFrame()
    if zm: s = s[s["zm"] == zm]
    key = {"rm": "rm", "zm": "zm", "dealer": "dealer_code"}[by]
    t = TARGETS["ROYAL ENFIELD"]
    g = s.groupby(key).agg(**({"Dealer": ("dealer_name", "first"), "RM": ("rm", "first")} if by == "dealer" else {}),
                           Dealers=("dealer_code", "nunique"), **{"MTD FY base": ("fy_base", "sum"), "MTD FY renewed": ("fy_ach", "sum")})
    g["MTD FY %"] = [ratio(a, b) for a, b in zip(g["MTD FY renewed"], g["MTD FY base"])]
    h = A.oem_frame(pack, "ROYAL ENFIELD", zm)
    if not h.empty:
        f = period_frames(h)
        hk = {"rm": "rm", "zm": "zm", "dealer": "dealer_code"}[by]
        y = A.rollup(f["ytd"], [hk]).set_index(hk)
        g["FYTD % (North)"] = y["Ret %"].reindex(g.index)
        g["FYTD FY % (North)"] = y["FY %"].reindex(g.index)
    g["FY target %"] = t["FY"]
    g["NOP to FY target"] = [max(0, math.ceil(t["FY"] * b - a)) if b else 0 for b, a in zip(g["MTD FY base"], g["MTD FY renewed"])]
    g["RAG"] = [A.rag(v, t["FY"]) for v in g["MTD FY %"]]
    if by == "dealer": g = g.drop(columns=["Dealers"])
    return g.reset_index().rename(columns={"rm": "RM", "zm": "ZM", "dealer_code": "Code"})


# --------------------------------------------------------------------------- #
# KPI tiles
# --------------------------------------------------------------------------- #
def period_tiles(s: dict) -> list[tuple]:
    t = s["target"]
    tiles = []
    for key, lab in (("ytd", s["ytd_label"]), ("lm", s["lm_label"]), ("mtd", s["mtd_label"])):
        m = s[key]
        if not _has(m): continue
        tiles.append((f"{lab} - overall", A.pct(m["Ret %"]), f"{A.n(m['Renewed'])} / {A.n(m['Base'])} · target {A.pct(t['TOT'])}"))
        tiles.append((f"{lab} - first year", A.pct(m["FY %"]), f"target {A.pct(t['FY'])}"))
        if m.get("OY base"):
            tiles.append((f"{lab} - other year", A.pct(m["OY %"]), f"target {A.pct(t['OY'])}"))
    return tiles


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #
def overall_dashboard(pack, zm=None) -> Report:
    rep = Report("Overall dashboard - all OEMs" + (f" - {zm.title()}" if zm else ""), A._asof_line(pack))
    rows, tiles = [], []
    for o in ["ROYAL ENFIELD"] + CAR_OEMS + ["PC"]:
        s = summary(pack, o, zm)
        if not (_has(s["ytd"]) or _has(s["mtd"])): continue
        lab = "Private car (all brands)" if o == "PC" else OEM_LABEL[o]
        y, m, l = s["ytd"], s["mtd"], s["lm"]
        if o != "PC":
            tiles.append((lab, f"YTD {A.pct(y['Ret %'])}" if _has(y) else "YTD -",
                          f"MTD {A.pct(m['Ret %'])} · target {A.pct(s['target']['TOT'])}"))
        rows.append({"OEM": lab, "YTD period": re.sub(r"^F?YTD ", "", s["ytd_label"]),
                     "YTD base": y["Base"], "YTD renewed": y["Renewed"], "YTD %": y["Ret %"] if _has(y) else None,
                     "FY YTD %": y["FY %"] if _has(y) else None, "OY YTD %": y["OY %"] if _has(y) else None,
                     "Last month %": l["Ret %"] if _has(l) else None, "MTD base": m["Base"], "MTD renewed": m["Renewed"],
                     "MTD %": m["Ret %"] if _has(m) else None, "MTD FY %": m["FY %"] if _has(m) else None,
                     "Target %": s["target"]["TOT"],
                     "Gap YTD (pts)": (y["Ret %"] - s["target"]["TOT"]) * 100 if _has(y) else None})
    rep.kpis(tiles, "Retention - year to date and month to date")
    tb = pd.DataFrame(rows)
    if len(tb):
        rep.table(tb, "OEM scorecard - YTD, last month and MTD")
        cp = tb[tb["OEM"] != "Private car (all brands)"]
        rep.chart(ChartSpec("bar", list(cp["OEM"]), {"YTD %": list(cp["YTD %"]), "MTD %": list(cp["MTD %"]),
                                                     "Target": list(cp["Target %"])}, "YTD vs MTD vs target by OEM", pct=True))
        rep.bullets(["Royal Enfield YTD is financial-year to date for the North zone (the only zone with monthly dealer "
                     "history); its MTD is national. Private-car YTD is Jan to the MIS date."])
    r = pack.get("retention")
    pc = r[r["book"] == "Private car"]
    if zm and not pc.empty: pc = pc[pc["zm"] == zm]
    if not pc.empty:
        t = A.rollup(pc, ["month", "oem"])
        piv = t.pivot(index="month", columns="oem", values="Ret %").sort_index()
        rep.chart(ChartSpec("line", [month_label(m) for m in piv.index], {OEM_LABEL.get(c, c): list(piv[c]) for c in piv.columns},
                            "Private car - retention % by month (current month = MTD)", pct=True))
        if not zm:
            z = ytd_mtd_table(pack, "ALL", ["zm"])
            if len(z):
                rep.h("Zones (ZM) - private car, YTD and MTD")
                rep.table(z.rename(columns={"zm": "ZM"}).sort_values("YTD %"))
    rep.h("Headlines")
    bl = []
    for o in ["ROYAL ENFIELD"] + CAR_OEMS:
        ins = A.re_headlines(pack) if o == "ROYAL ENFIELD" else A.retention_insights(A.oem_frame(pack, o, zm), o)
        if ins: bl.append(f"**{OEM_LABEL[o]}** - " + " ".join(i.replace("**", "") for i in ins[:2]))
    rep.bullets(bl)
    dr = defaulter_table(pack, None, zm)
    pay = A.payouts_summary(pack)
    if pay is not None:
        rep.h("Payouts")
        rep.kpis([("Defaulter RMs", A.n(len(dr)), "2 or more unpaid invoices"),
                  ("Unpaid invoices", A.n(pay["Invoices pending"].sum()), ""),
                  ("Pending value", A.inr(pay["Pending value"].sum()), "base value")])
        rep.table(pay, "Payouts pending by book")
    return rep


def oem_dashboard(pack, oem: str, zm=None, rm=None) -> Report:
    s = summary(pack, oem, zm, rm)
    lab = OEM_LABEL[oem]
    scope = " / ".join(x.title() for x in (zm, rm) if x)
    rep = Report(f"{lab} dashboard" + (f" - {scope}" if scope else ""),
                 A._asof_line(pack, "Royal Enfield" if oem == "ROYAL ENFIELD" else "Private car"))
    rep.kpis(period_tiles(s), "Year to date · last month · month to date")
    if s["note"]: rep.bullets([s["note"]])
    if oem == "ROYAL ENFIELD":
        base = A.re_report(pack, zm, rm)
        rep.extend(Report("", "", base.blocks))
        if not rm:
            t = re_group_table(pack, "rm", zm)
            if len(t):
                rep.h("RMs - national MTD first year and North FYTD")
                rep.table(t.sort_values("MTD FY %"))
        return rep
    base = A.oem_report(pack, oem, zm, rm, extras=False)
    blocks = base.blocks[1:] if base.blocks and base.blocks[0].kind == "kpis" else base.blocks
    rep.extend(Report("", "", blocks))
    if not rm:
        if not zm:
            z = ytd_mtd_table(pack, oem, ["zm"])
            if len(z):
                rep.h("Zones (ZM) - YTD, last month and MTD")
                rep.table(z.rename(columns={"zm": "ZM"}).sort_values("YTD %"))
        r_ = ytd_mtd_table(pack, oem, ["zm", "rm"], zm)
        if len(r_):
            rep.h("RMs - YTD, last month and MTD")
            rep.chart(from_df(r_[r_["YTD base"] >= A.MIN_BASE].sort_values("YTD %"), "rm", "YTD %", "hbar",
                              f"{lab} - YTD retention % by RM", pct=True, target=s["target"]["TOT"]))
            rep.table(r_.rename(columns={"zm": "ZM", "rm": "RM"}).sort_values("YTD %"))
    return rep


def rm_page(pack, oem: str, zm=None) -> Report:
    """RM scorecard with YTD / LM / MTD, per OEM (or every OEM for Overall)."""
    rep = Report(f"RM scorecard - {PAGE_LABEL.get(oem, oem)}" + (f" - {zm.title()}" if zm else ""),
                 "Year to date, last month and month to date per RM - click a row for the RM dashboard")
    oems = ["ROYAL ENFIELD"] + CAR_OEMS if oem == "OVERALL" else [oem]
    for o in oems:
        t = re_group_table(pack, "rm", zm) if o == "ROYAL ENFIELD" else ytd_mtd_table(pack, o, ["zm", "rm"], zm)
        if t.empty: continue
        if len(oems) > 1: rep.h(OEM_LABEL[o])
        t = t.rename(columns={"zm": "ZM", "rm": "RM"})
        t = t[t["RM"].astype(str) != ""]
        rep.table(t.sort_values("MTD FY %" if o == "ROYAL ENFIELD" else "YTD %"))
    return rep


def dealer_page(pack, oem: str, zm=None, rm=None, text=None, rag=None) -> Report:
    rep = Report(f"Dealers - {PAGE_LABEL.get(oem, oem)}" + (f" - {zm.title()}" if zm else ""),
                 "YTD, last month and MTD per dealer, worst YTD gap first - click a row for the dealer dashboard")
    oems = ["ROYAL ENFIELD"] + CAR_OEMS if oem == "OVERALL" else [oem]
    for o in oems:
        if o == "ROYAL ENFIELD":
            t = re_group_table(pack, "dealer", zm)
            if not t.empty and rm: t = t[t["RM"] == rm]
            sort = ("NOP to FY target", False)
        else:
            t = ytd_mtd_table(pack, o, ["dealer_code", "dealer_name", "zm", "rm"], zm, rm)
            if not t.empty:
                t = t.rename(columns={"dealer_code": "Code", "dealer_name": "Dealer", "zm": "ZM", "rm": "RM"})
                t = t[t["YTD base"] >= A.MIN_BASE] if not text else t
            sort = ("NOP to target", False)
        if t.empty: continue
        if text:
            t = t[(t["Dealer"].astype(str) + " " + t["Code"].astype(str)).str.contains(text, case=False, regex=False)]
        if rag: t = t[t["RAG"].isin(rag)]
        if t.empty: continue
        if len(oems) > 1: rep.h(OEM_LABEL[o])
        rep.table(t.sort_values(sort[0], ascending=sort[1]))
    if not rep.blocks: rep.p("No dealers match these filters.")
    return rep


# --------------------------------------------------------------------------- #
# payouts: defaulter RMs (2 or more unpaid invoices, any reason)
# --------------------------------------------------------------------------- #
DEFAULT_MIN_INVOICES = 2
REASONS = [("Invoice not uploaded", r"not uploaded|num not updated"), ("Discrepancy / re-upload", r"discrepanc|re-upload"),
           ("GSTIN hold", r"gstin"), ("Dealer deactivation", r"deactivation"), ("Awaiting physical invoice", r"physical"),
           ("Branch / Ops hold", r"branch|ops"), ("Bank / PSU merger", r"bank|psu"), ("Scheduled for payment", r"payout on")]


def _reason(status: str) -> str:
    for lab, rx in REASONS:
        if re.search(rx, status or "", re.I): return lab
    return "Other"


def unpaid_invoices(pack, oem=None, zm=None) -> pd.DataFrame:
    """One row per unpaid invoice (invoice lines combined). Rows without a usable invoice number count on their own."""
    parts = []
    for t in (["re_payouts"] if oem == "ROYAL ENFIELD" else ["pc_payouts"] if oem else ["re_payouts", "pc_payouts"]):
        d = pack.get(t)
        if d.empty: continue
        if oem: d = d[d["oem"] == oem]
        parts.append(d[~d["paid"]])
    if not parts: return pd.DataFrame()
    u = pd.concat(parts, ignore_index=True)
    if zm: u = u[u["zm"] == zm]
    if u.empty: return u
    inv = u["invoice_no"].astype(str).str.strip() if "invoice_no" in u else pd.Series("", index=u.index)
    ok = ~inv.isin(["", "0", "nan", "None", "NA", "-"])
    u = u.assign(inv_key=[f"{o}|{c}|{i}" if good else f"row{ix}" for ix, o, c, i, good in
                          zip(u.index, u["oem"], u["dealer_code"], inv, ok)])
    g = u.groupby("inv_key").agg(oem=("oem", "first"), rm=("rm", "first"), zm=("zm", "first"), dealer_code=("dealer_code", "first"),
                                 dealer_name=("dealer_name", "first"), invoice_no=("invoice_no", "first"), fy=("fy", "first"),
                                 month=("month", "min"), status=("status", "first"), value=("base_value", "sum"))
    g["reason"] = g["status"].map(_reason)
    g["rm"] = g["rm"].fillna("").astype(str)
    return g.reset_index(drop=True)


def defaulter_table(pack, oem=None, zm=None, min_invoices=DEFAULT_MIN_INVOICES) -> pd.DataFrame:
    u = unpaid_invoices(pack, oem, zm)
    if u.empty: return pd.DataFrame()
    u = u[u["rm"] != ""]
    g = u.groupby("rm").agg(ZM=("zm", lambda s: ", ".join(sorted({x for x in s if x})[:2])),
                            OEMs=("oem", lambda s: ", ".join(sorted({OEM_LABEL.get(x, x) for x in s}))),
                            **{"Unpaid invoices": ("inv_key" if "inv_key" in u else "invoice_no", "size"),
                               "Dealers": ("dealer_code", "nunique"), "Pending value": ("value", "sum"),
                               "Oldest month": ("month", "min")})
    top = u.groupby(["rm", "reason"]).size().reset_index(name="k").sort_values("k", ascending=False).drop_duplicates("rm")
    g["Main reason"] = top.set_index("rm")["reason"].reindex(g.index)
    g = g[g["Unpaid invoices"] >= min_invoices].sort_values(["Unpaid invoices", "Pending value"], ascending=False)
    g["Oldest month"] = [month_label(m) if isinstance(m, str) else "" for m in g["Oldest month"]]
    return g.reset_index().rename(columns={"rm": "RM"})


def defaulter_report(pack, oem=None, zm=None) -> Report:
    lab = PAGE_LABEL.get(oem or "OVERALL", oem)
    rep = Report(f"Defaulter RMs - {lab}" + (f" - {zm.title()}" if zm else ""),
                 f"An RM is a defaulter when {DEFAULT_MIN_INVOICES} or more invoices in their book are unpaid, for any reason")
    u = unpaid_invoices(pack, None if oem in (None, "OVERALL") else oem, zm)
    t = defaulter_table(pack, None if oem in (None, "OVERALL") else oem, zm)
    if u.empty:
        return rep.p("No unpaid invoices in this scope.")
    rep.kpis([("Defaulter RMs", A.n(len(t)), f"of {A.n(u.loc[u['rm'] != '', 'rm'].nunique())} RMs with unpaid invoices"),
              ("Their unpaid invoices", A.n(t["Unpaid invoices"].sum()) if len(t) else "0", ""),
              ("Pending value", A.inr(t["Pending value"].sum()) if len(t) else "₹0", "base value"),
              ("Invoices with no RM", A.n((u["rm"] == "").sum()), "fix the RM mapping")])
    if t.empty:
        return rep.p("No RM has 2 or more unpaid invoices.")
    rep.chart(from_df(t.head(20), "RM", "Unpaid invoices", "hbar", "Unpaid invoices by RM", pct=False))
    rep.table(t, "Defaulter RMs")
    mix = u[u["rm"].isin(t["RM"])].pivot_table(index="rm", columns="reason", values="value", aggfunc="size", fill_value=0)
    mix = mix.reindex(t["RM"]).reset_index().rename(columns={"rm": "RM"})
    rep.table(mix, "Why they are unpaid - invoices by reason")
    det = u[u["rm"].isin(t["RM"])].groupby(["rm", "dealer_code", "dealer_name"], as_index=False).agg(
        Invoices=("value", "size"), Value=("value", "sum"), Reason=("reason", lambda s: s.value_counts().index[0]))
    rep.table(det.sort_values(["rm", "Invoices"], ascending=[True, False]).rename(
        columns={"rm": "RM", "dealer_code": "Code", "dealer_name": "Dealer"}), "Dealers behind each defaulter RM")
    rep.bullets(["Invoice not uploaded / discrepancy / physical invoice: the dealer acts, the RM chases.",
                 "GSTIN hold / deactivation: the dealer fixes the registration; the RM escalates to the branch.",
                 "Scheduled for payment: finance has it - no RM action, it clears on the payout date."], "Who acts")
    return rep


def payout_page(pack, oem: str, zm=None) -> Report:
    if oem == "OVERALL":
        from . import ra
        return ra.payout_report(pack, zm)
    rep = Report(f"Payout status - {OEM_LABEL[oem]}" + (f" - {zm.title()}" if zm else ""), "Pending invoices and what is already paid")
    sub = A.payout_section(pack, "re_payouts" if oem == "ROYAL ENFIELD" else "pc_payouts",
                           None if oem == "ROYAL ENFIELD" else oem, zm)
    if not sub.blocks: return rep.p("No pending payouts.")
    return rep.extend(sub)


# --------------------------------------------------------------------------- #
# Power BI toolkit
# --------------------------------------------------------------------------- #
DIMS = {"OEM": "oem", "ZM": "zm", "RM": "rm", "Dealer": "dealer_name", "Dealer code": "dealer_code", "Month": "month",
        "Segment (FY/OY)": "segment", "Branch": "branch", "Region": "region"}
MEASURES = ["Retention %", "Base (due)", "Renewed", "Not renewed", "Renewals to target", "FY %", "OY %"]
PERIODS = ["YTD", "MTD", "Last closed month", "All months"]


def explore(pack, page: str, rows: str, cols: str | None, measure: str, period: str, zm=None) -> tuple[pd.DataFrame, Report]:
    """Power BI-style matrix: rows x columns x measure over the retention fact, with a chart."""
    r = pack.get("retention")
    if page == "OVERALL":
        d = r
    else:
        d = r[r["oem"] == page]
    if zm: d = d[d["zm"] == zm]
    if d.empty: return pd.DataFrame(), Report("Explore").p("No data.")
    if period != "All months":
        parts = []
        for _, x in d.groupby("oem"):
            f = period_frames(x)
            parts.append(f["ytd"] if period == "YTD" else f["mtd"] if period == "MTD" else f["lm"])
        d = pd.concat(parts) if parts else d.iloc[0:0]
    rk, ck = DIMS[rows], (DIMS[cols] if cols and cols != "(none)" else None)
    by = [rk] + ([ck] if ck and ck != rk else [])
    g = A.rollup(d, by)
    g["Not renewed"] = g["Base"] - g["Renewed"]
    tgt = {o: TARGETS.get(o, TARGETS["SKODA"])["TOT"] for o in set(d["oem"])}
    tv = max(tgt.values()) if len(tgt) == 1 else None
    g["Renewals to target"] = [max(0, math.ceil((tv or 0.5) * b - a)) for b, a in zip(g["Base"], g["Renewed"])]
    col = {"Retention %": "Ret %", "Base (due)": "Base", "Renewed": "Renewed", "Not renewed": "Not renewed",
           "Renewals to target": "Renewals to target", "FY %": "FY %", "OY %": "OY %"}[measure]
    pct = col.endswith("%")
    if rk == "oem" or ck == "oem":
        g["oem"] = g["oem"].map(lambda x: OEM_LABEL.get(x, x))
    if rk == "month": g["month"] = g["month"].map(month_label)
    if len(by) == 2:
        if ck == "month":
            order = sorted(set(d["month"]))
            g[ck] = g[ck].map(month_label); cols_order = [month_label(m) for m in order]
        else:
            cols_order = None
        m = g.pivot_table(index=rk, columns=ck, values=col, aggfunc="sum")
        if cols_order: m = m.reindex(columns=[c for c in cols_order if c in m.columns])
        if not pct: m["Total"] = m.sum(axis=1)
        m = m.reset_index()
        m.columns.name = None
    else:
        m = g[[rk, "Base", "Renewed", "Ret %", "FY %", "OY %", "Renewals to target"]].copy()
        if rk == "month":
            pass
        else:
            m = m.sort_values(col if col in m else "Ret %", ascending=pct)   # worst first
    m = m.rename(columns={v: k for k, v in DIMS.items()})
    title = f"{measure} by {rows}" + (f" and {cols}" if len(by) == 2 else "") + f" - {period}" + \
            (f" - {PAGE_LABEL.get(page, page)}" if page else "")
    rep = Report(title, "Built in the Power BI explorer")
    x = m.columns[0]
    if len(by) == 2:
        series = {str(c): list(m[c]) for c in m.columns[1:] if c != "Total"}
        if len(series) <= 12 and len(m) <= 40:
            rep.chart(ChartSpec("line" if rk == "Month" else "bar", [str(v) for v in m[x]], series, title, pct=pct))
    else:
        yv = col if col in m.columns else "Ret %"
        top = m.head(30)
        rep.chart(ChartSpec("line" if rows == "Month" else "hbar", [str(v) for v in top[x]], {measure: list(top[yv])}, title,
                            pct=pct, target=tv if pct and col == "Ret %" else None))
    rep.table(m, title)
    return m, rep


DAX = r"""// ---- Measures for the MIS model (paste into Power BI: Modeling > New measure) ----
Base = SUM ( fact_retention[base] )
Renewed = SUM ( fact_retention[ach] )
Not Renewed = [Base] - [Renewed]
Retention % = DIVIDE ( [Renewed], [Base] )
FY Retention % = CALCULATE ( [Retention %], fact_retention[segment] = "FY" )
OY Retention % = CALCULATE ( [Retention %], fact_retention[segment] = "OY" )
Target % = MAX ( dim_oem[target_total] )
Gap vs Target (pts) = ( [Retention %] - [Target %] ) * 100
Renewals to Target = MAX ( 0, ROUNDUP ( [Target %] * [Base] - [Renewed], 0 ) )

// Periods - the running month is flagged by fact_retention[is_mtd]
MTD Retention % = CALCULATE ( [Retention %], fact_retention[is_mtd] = TRUE () )
Last Month Retention % =
    VAR lm = CALCULATE ( MAX ( dim_month[month_start] ), fact_retention[is_mtd] = FALSE (), ALL ( dim_month ) )
    RETURN CALCULATE ( [Retention %], dim_month[month_start] = lm, ALL ( dim_month ) )
YTD Retention % =
    VAR lastm = CALCULATE ( MAX ( dim_month[month_start] ), ALL ( dim_month ) )
    VAR ystart = IF ( MAX ( dim_oem[fiscal_start_month] ) = 4,
                      DATE ( YEAR ( lastm ) - IF ( MONTH ( lastm ) < 4, 1, 0 ), 4, 1 ), DATE ( YEAR ( lastm ), 1, 1 ) )
    RETURN CALCULATE ( [Retention %], dim_month[month_start] >= ystart, dim_month[month_start] <= lastm, ALL ( dim_month ) )
RAG = VAR r = [Retention %] VAR t = [Target %]
      RETURN IF ( ISBLANK ( r ), BLANK (), IF ( r >= t, "Green", IF ( r >= t - 0.10, "Amber", "Red" ) ) )

// Payouts
Unpaid Invoices = CALCULATE ( DISTINCTCOUNT ( fact_payouts[invoice_key] ), fact_payouts[paid] = FALSE () )
Pending Value = CALCULATE ( SUM ( fact_payouts[base_value] ), fact_payouts[paid] = FALSE () )
Is Defaulter RM = IF ( [Unpaid Invoices] >= 2, "Defaulter", "OK" )

// Penetration (never add to retention)
Penetration % = DIVIDE ( SUM ( fact_penetration[new_nop] ), SUM ( fact_penetration[retail] ) )
"""

GUIDE = """MIS model for Power BI
======================

Files
- MIS_model.xlsx        every table below as one sheet (easiest: Get data > Excel workbook > tick all sheets)
- csv/*.csv             the same tables as CSV (for Power BI dataflows or a SharePoint/Drive folder source)
- measures.dax          all measures - paste each one via Modeling > New measure
- relationships.txt     the star schema to set up in Model view

Tables
- fact_retention   one row per OEM x dealer x month x segment (FY/OY): base = due, ach = renewed, is_mtd = running month
- fact_payouts     one row per payout invoice line: status, paid, base_value, rm, zm, invoice_key (count distinct)
- fact_penetration Audi/Skoda/VW new-car policies vs cars sold, by dealer and month
- dim_dealer, dim_rm, dim_month, dim_oem (targets and fiscal start month: RE = April, cars = January)

Rules (same as the app)
- Retention % = SUM(renewed) / SUM(base), never the average of percentages.
- The running month is month to date - compare it with the same date last month, not with closed months.
- Royal Enfield dealer history is the North zone only.
- Penetration and retention are different books - never add them.

Refresh: re-download this package after each MIS drop and use Home > Refresh (keep the same file paths), or point
the Folder source at the synced Google Drive folder.
"""

RELS = """dim_dealer[dealer_key] 1 --- * fact_retention[dealer_key]
dim_month[month]        1 --- * fact_retention[month]
dim_oem[oem]            1 --- * fact_retention[oem]
dim_rm[rm]              1 --- * fact_retention[rm]
dim_oem[oem]            1 --- * fact_payouts[oem]
dim_rm[rm]              1 --- * fact_payouts[rm]
dim_month[month]        1 --- * fact_payouts[month]
dim_month[month]        1 --- * fact_penetration[month]
(single direction, dimension filters fact)
"""


def powerbi_tables(pack) -> dict[str, pd.DataFrame]:
    r = pack.get("retention").copy()
    r["dealer_key"] = r["oem"] + "|" + r["dealer_code"].astype(str)
    fact = r[["dealer_key", "book", "oem", "dealer_code", "zm", "rm", "branch", "region", "month", "is_mtd", "segment", "base", "ach"]]
    dealers = r.drop_duplicates("dealer_key")[["dealer_key", "oem", "dealer_code", "dealer_name", "zm", "rm", "branch", "region"]]
    snap = pack.get("re_dealer_snapshot")
    if not snap.empty:
        extra = snap.assign(dealer_key="ROYAL ENFIELD|" + snap["dealer_code"].astype(str), oem="ROYAL ENFIELD", branch="")[
            ["dealer_key", "oem", "dealer_code", "dealer_name", "zm", "rm", "branch", "region"]]
        dealers = pd.concat([dealers, extra[~extra["dealer_key"].isin(dealers["dealer_key"])]], ignore_index=True)
    months = sorted(set(r["month"]) | set(pack.get("pc_payouts").get("month", pd.Series(dtype=str)).dropna())
                    | set(pack.get("re_payouts").get("month", pd.Series(dtype=str)).dropna()))
    dim_month = pd.DataFrame({"month": months})
    dim_month["month_start"] = pd.to_datetime(dim_month["month"] + "-01", errors="coerce")
    dim_month["label"] = dim_month["month"].map(month_label)
    dim_oem = pd.DataFrame([{"oem": o, "label": OEM_LABEL.get(o, o), "target_fy": t["FY"], "target_oy": t["OY"],
                             "target_total": t["TOT"], "fiscal_start_month": 4 if o == "ROYAL ENFIELD" else 1}
                            for o, t in TARGETS.items()])
    pays = []
    for t in ("re_payouts", "pc_payouts"):
        d = pack.get(t)
        if d.empty: continue
        inv = d["invoice_no"].astype(str).str.strip() if "invoice_no" in d else pd.Series("", index=d.index)
        ok = ~inv.isin(["", "0", "nan", "None"])
        pays.append(d.assign(invoice_key=[f"{o}|{c}|{i}" if g else f"{t}-row{ix}" for ix, o, c, i, g in
                                          zip(d.index, d["oem"], d["dealer_code"], inv, ok)]))
    fp = pd.concat(pays, ignore_index=True) if pays else pd.DataFrame()
    rms = sorted({x for x in list(fact["rm"]) + list(fp.get("rm", [])) if x})
    rm_zm = fact[fact["rm"] != ""].drop_duplicates("rm").set_index("rm")["zm"]
    dim_rm = pd.DataFrame({"rm": rms, "zm": [rm_zm.get(x, "") for x in rms]})
    out = {"fact_retention": fact, "fact_payouts": fp, "fact_penetration": pack.get("pc_penetration_month"),
           "dim_dealer": dealers, "dim_rm": dim_rm, "dim_month": dim_month, "dim_oem": dim_oem}
    return {k: v for k, v in out.items() if v is not None and not v.empty}


def powerbi_package(pack) -> bytes:
    """Zip: MIS_model.xlsx + csv/ + measures.dax + relationships.txt + README - a ready star schema for Power BI."""
    tabs = powerbi_tables(pack)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        xb = io.BytesIO()
        with pd.ExcelWriter(xb, engine="openpyxl") as w:
            for k, v in tabs.items():
                v.to_excel(w, sheet_name=k[:31], index=False)
        z.writestr("MIS_model.xlsx", xb.getvalue())
        for k, v in tabs.items():
            z.writestr(f"csv/{k}.csv", v.to_csv(index=False))
        z.writestr("measures.dax", DAX)
        z.writestr("relationships.txt", RELS)
        z.writestr("README.txt", GUIDE)
        z.writestr("model.json", json.dumps({k: list(v.columns) for k, v in tabs.items()}, indent=1))
    return buf.getvalue()


def re_first_year(pack, zm=None, rm=None) -> Report:
    """Royal Enfield only: first-year conversion by region, RM and dealer (no private-car rows)."""
    t = TARGETS["ROYAL ENFIELD"]
    scope = " · ".join(x.title() for x in (zm, rm) if x)
    rep = Report("Royal Enfield - first-year conversion" + (f" - {scope}" if scope else ""),
                 "Month to date, national; vs the same date last month")
    rg = pack.get("re_region")
    if not rg.empty and not rm:
        if zm: rg = rg[[re_zm(x) == zm for x in rg["region"]]]
        g = rg.assign(ZM=[re_zm(x) for x in rg["region"]])
        g = g.assign(**{"FY %": [ratio(a, b) for a, b in zip(g["fy_ach"], g["fy_base"])],
                        "Same date last month %": [ratio(a, b) for a, b in zip(g["fy_ach_prev"], g["fy_base_prev"])]})
        g["Change (pts)"] = (g["FY %"] - g["Same date last month %"]) * 100
        g["To target"] = [max(0, math.ceil(t["FY"] * b - a)) for a, b in zip(g["fy_ach"], g["fy_base"])]
        g = g.sort_values("FY %")
        rep.chart(ChartSpec("hbar", list(g["region"]), {"First year MTD": list(g["FY %"])}, "First-year conversion by region",
                            pct=True, target=t["FY"]))
        rep.table(g[["ZM", "region", "state", "fy_base", "fy_ach", "FY %", "Same date last month %", "Change (pts)", "To target"]]
                  .rename(columns={"region": "Region", "state": "State", "fy_base": "FY due", "fy_ach": "FY renewed"}), "By region")
    by = "dealer" if rm else "rm"
    gt = re_group_table(pack, by, zm)
    if rm and not gt.empty: gt = gt[gt["RM"] == rm]
    if not gt.empty:
        rep.table(gt.sort_values("NOP to FY target", ascending=False).head(40), "By dealer" if rm else "By RM")
    return rep
