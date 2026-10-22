"""Management view: retention AND penetration for one scope (Overall / OEM / ZM / RM), in a compact form.

`overview()` returns a view model (KPIs, a combined trend, a scorecard one level down, attention / best lists and at
most three key messages). The app renders it as a management dashboard; `to_report()` turns the same numbers into a
Report so it exports to HTML / PDF / PPT / CSV.

Scorecard level: Overall -> OEMs, OEM -> ZMs, ZM -> RMs, RM -> dealers (clubbed groups as line items then total).

Penetration = new policies sold with the vehicle / vehicles sold (retail). Cars: SAVWIPL penetration MIS (Audi, Skoda,
VW; Volvo is not in it). Royal Enfield: program policies / bikes sold from the RE penetration report (MTD and YTD).
Penetration and retention are separate books - they sit side by side and are never added.
"""
from __future__ import annotations

import datetime as dt
import math

import pandas as pd

from . import analytics as A
from . import groups as G
from .charts import ChartSpec
from .model import OEM_LABEL, TARGETS, re_zm
from .report import Report
from .util import month_label, ratio
from .views import CAR_OEMS, PAGE_LABEL, period_frames, re_due_so_far, re_group_table, summary, ytd_mtd_table


def _is_mtd(c) -> bool:
    """The running-month column of ytd_mtd_table: 'MTD Oct'26 %' or 'Oct'26 to date %'."""
    c = str(c)
    return c.startswith("MTD") or c.endswith("to date %")

ORDER = ["ROYAL ENFIELD", "AUDI", "SKODA", "VOLKSWAGEN", "VOLVO"]


# --------------------------------------------------------------------------- #
# penetration
# --------------------------------------------------------------------------- #
def pc_pen_frame(pack, oem, zm=None, rm=None) -> pd.DataFrame:
    p = pack.get("pc_penetration_month")
    if p.empty: return p
    p = p[p["oem"].isin(CAR_OEMS if oem in ("OVERALL", "PC") else [oem])]
    if zm: p = p[p["zm"] == zm]
    if rm: p = p[p["rm"] == rm]
    return p


def re_pen_frame(pack, zm=None, rm=None) -> pd.DataFrame:
    p = pack.get("re_penetration")
    if p.empty: return p
    if zm: p = p[[re_zm(x) == zm for x in p["region"]]]
    if rm: p = p[p["rm"] == rm]
    return p


def pen_summary(pack, oem, zm=None, rm=None) -> dict | None:
    """{'ytd','latest','prev': fraction, labels, 'sold','policies'} or None when the OEM has no penetration data."""
    if oem == "ROYAL ENFIELD":
        p = re_pen_frame(pack, zm, rm)
        if p.empty: return None
        return {"ytd": ratio(p["prog_ytd"].sum(), p["veh_ytd"].sum()), "latest": ratio(p["prog_mtd"].sum(), p["veh_mtd"].sum()),
                "prev": ratio(p["prog_prev"].sum(), p["veh_prev"].sum()) if "veh_prev" in p else None,
                "ytd_label": "Pen. YTD", "latest_label": "Pen. MTD", "prev_label": "last month",
                "sold": p["veh_ytd"].sum(), "policies": p["prog_ytd"].sum(),
                "addon_ytd": ratio(p["addon_ytd"].sum(), p["prog_ytd"].sum()) if "addon_ytd" in p else None,
                "addon_mtd": ratio(p["addon_mtd"].sum(), p["prog_mtd"].sum()) if "addon_mtd" in p else None}
    p = pc_pen_frame(pack, oem, zm, rm)
    if p.empty: return None
    last = p["month"].max()
    y = p[p["month"].str[:4] == last[:4]]
    l = p[p["month"] == last]
    months = sorted(p["month"].unique())
    pv = p[p["month"] == months[-2]] if len(months) > 1 else p.iloc[0:0]
    return {"ytd": ratio(y["new_nop"].sum(), y["retail"].sum()), "latest": ratio(l["new_nop"].sum(), l["retail"].sum()),
            "prev": ratio(pv["new_nop"].sum(), pv["retail"].sum()) if len(pv) else None,
            "ytd_label": f"Pen. YTD (to {month_label(last)})", "latest_label": f"Pen. {month_label(last)}",
            "prev_label": month_label(months[-2]) if len(months) > 1 else "", "sold": y["retail"].sum(), "policies": y["new_nop"].sum()}


def pen_by(pack, oem, key, zm=None, rm=None) -> pd.DataFrame:
    """Penetration YTD and latest month per key (zm / rm / dealer_code)."""
    if oem == "ROYAL ENFIELD":
        p = re_pen_frame(pack, zm, rm)
        if p.empty: return pd.DataFrame()
        p = p.assign(zm=[re_zm(x) for x in p["region"]])
        g = p.groupby(key).agg(sold=("veh_ytd", "sum"), pol=("prog_ytd", "sum"), sold_l=("veh_mtd", "sum"), pol_l=("prog_mtd", "sum"))
    else:
        p = pc_pen_frame(pack, oem, zm, rm)
        if p.empty: return pd.DataFrame()
        if key == "dealer_code": p = p.assign(dealer_code=p["dealer_code"].map(G.parent_code))
        last = p["month"].max()
        y = p[p["month"].str[:4] == last[:4]]
        g = y.groupby(key).agg(sold=("retail", "sum"), pol=("new_nop", "sum"))
        l = p[p["month"] == last].groupby(key).agg(sold_l=("retail", "sum"), pol_l=("new_nop", "sum"))
        g = g.join(l, how="left").fillna(0)
    g["Pen YTD %"] = [ratio(a, b) for a, b in zip(g["pol"], g["sold"])]
    g["Pen latest %"] = [ratio(a, b) for a, b in zip(g["pol_l"], g["sold_l"])]
    return g.rename(columns={"sold": "Vehicles sold", "pol": "Policies", "sold_l": "_sl", "pol_l": "_pl"})


# --------------------------------------------------------------------------- #
# the overview
# --------------------------------------------------------------------------- #
def inr_short(x) -> str:
    x = float(x or 0)
    return f"₹{x / 1e7:,.2f} Cr" if x >= 1e7 else f"₹{x / 1e5:,.1f} L" if x >= 1e5 else f"₹{x:,.0f}"


def _status(v, t):
    if v is None or v != v: return ""
    return "good" if v >= t else "warn" if v >= t - A.AMBER else "bad"


def kpis(pack, oem, zm=None, rm=None) -> list[dict]:
    """KPI cards for the strip. Overall: one per OEM. OEM scope: retention overall / FY / OY, penetration, to target."""
    cards = []
    if oem == "OVERALL":
        for o in ORDER:
            s = summary(pack, o, zm, rm); pn = pen_summary(pack, o, zm, rm)
            y, m = s["ytd"], s["mtd"]
            if not (y["Base"] or m["Base"]): continue
            main = y["Ret %"] if y["Base"] else m["Ret %"]
            per = s["ytd_label"] if y["Base"] else s["mtd_label"]
            lines = [(s["lm_label"], s["lm"]["Ret %"] if s["lm"]["Base"] else None), (s["mtd_label"], m["Ret %"] if m["Base"] else None)]
            if o == "ROYAL ENFIELD" and not y["Base"]:
                dsf = re_due_so_far(pack, zm, rm)
                if dsf.get("due"):
                    main, per = dsf["conv"], s["mtd_label"] + " · on policies due so far"
                    lines = [("same date last month", dsf["prev"]), ("of whole month", m["Ret %"] if m["Base"] else None)]
            cards.append({"title": OEM_LABEL[o], "per": per, "value": main,
                          "target": s["target"]["TOT"], "status": _status(main, s["target"]["TOT"]),
                          "lines": lines, "pen": (pn["ytd_label"].split(" (")[0], pn["ytd"]) if pn else None})
        return cards
    s = summary(pack, oem, zm, rm); t = s["target"]; pn = pen_summary(pack, oem, zm, rm)
    y, m, l = s["ytd"], s["mtd"], s["lm"]
    base = y if y["Base"] else m
    if oem == "ROYAL ENFIELD":
        dsf = re_due_so_far(pack, zm, rm)
        if dsf.get("due"):
            cards.append({"title": "Conversion", "per": s["mtd_label"] + " · on policies due so far", "value": dsf["conv"],
                          "target": t["TOT"], "status": _status(dsf["conv"], t["TOT"]),
                          "lines": [("same date last month", dsf["prev"]), ("of whole month", m["Ret %"] if m.get("Base") else None)],
                          "sub": f"{dsf['renewed']:,.0f} renewed of {dsf['due']:,.0f} due so far"})
    for k, lab, tg in (("Ret %", "Retention", t["TOT"]), ("FY %", "First year", t["FY"]), ("OY %", "Other year", t["OY"])):
        if oem == "ROYAL ENFIELD" and k == "Ret %" and any(c["title"] == "Conversion" for c in cards): continue
        bk = {"Ret %": "Base", "FY %": "FY base", "OY %": "OY base"}[k]
        if not base.get(bk): continue
        cards.append({"title": lab, "per": (s["ytd_label"] if y["Base"] else s["mtd_label"] + (" · of whole month" if oem == "ROYAL ENFIELD" else "")),
                      "value": base[k], "target": tg,
                      "status": _status(base[k], tg),
                      "lines": [(s["lm_label"], l[k] if l.get(bk) else None), (s["mtd_label"], m[k] if m.get(bk) else None)]})
    if pn:
        cards.append({"title": "Penetration", "per": pn["ytd_label"], "value": pn["ytd"], "target": None, "status": "",
                      "lines": [(pn["latest_label"].replace("Pen. ", ""), pn["latest"]), (pn["prev_label"], pn["prev"])] if pn["prev_label"]
                      else [(pn["latest_label"].replace("Pen. ", ""), pn["latest"])],
                      "sub": f"{pn['policies']:,.0f} policies on {pn['sold']:,.0f} vehicles"})
        if pn.get("addon_ytd") is not None:
            cards.append({"title": "Add-on penetration", "per": "policies with an add-on cover · YTD", "value": pn["addon_ytd"],
                          "target": None, "status": "", "lines": [("MTD", pn.get("addon_mtd"))], "sub": "share of program policies"})
    if oem == "VOLVO":
        nc = volvo_ncb(pack, zm, rm)
        if nc:
            for flag, lab in (("NCB", "NCB customers"), ("Non-NCB", "Non-NCB customers")):
                q = nc[flag]
                if q["base"]:
                    cards.append({"title": lab, "per": q["label"], "value": q["ytd"], "target": t["TOT"], "status": _status(q["ytd"], t["TOT"]),
                                  "lines": [("first yr", q["fy"]), ("other yr", q["oy"]), ("MTD", q["mtd"])],
                                  "sub": f"{q['ach']:,.0f} of {q['base']:,.0f}"})
        sn = volvo_safenet(pack, zm, rm)
        if sn and sn["share"] is not None:
            cards.append({"title": "Safenet share of renewals", "per": f"policies renewed {sn['renewed_label']}", "value": sn["share"],
                          "target": None, "status": "", "lines": [], "sub": f"{sn['safenet']:,.0f} Safenet of {sn['safenet'] + sn['non_safenet']:,.0f} renewed"})
    if oem == "ROYAL ENFIELD":
        ea = re_earnings(pack, zm, rm)
        if ea:
            cards.append({"title": "Earnings (this month)", "per": f"Rs {ea['rate']} per retained policy", "value": None,
                          "text": inr_short(ea["earned"]), "status": "", "lines": [],
                          "sub": f"at target {inr_short(ea['at_target'])} · still on the table {inr_short(ea['left'])}"})
    need = max(0, math.ceil(t["TOT"] * base["Base"] - base["Renewed"]))
    sub = f"{base['Renewed']:,.0f} renewed of {base['Base']:,.0f} due"
    if oem == "ROYAL ENFIELD":
        dsf = re_due_so_far(pack, zm, rm)
        if dsf.get("due"):
            need = max(0, math.ceil(t["TOT"] * dsf["due"] - dsf["renewed"]))
            sub = f"on the {dsf['due']:,.0f} policies due so far this month"
    cards.append({"title": "Renewals to target", "per": s["ytd_label"] if y["Base"] else s["mtd_label"], "value": None,
                  "count": need, "status": "", "lines": [], "sub": sub})
    return cards


def trend(pack, oem, zm=None, rm=None) -> ChartSpec | None:
    """Retention % and penetration % by month on one chart (both are percentages)."""
    if oem == "OVERALL":
        r = pack.get("retention")
        pc = r[r["oem"].isin(CAR_OEMS)]
        if zm: pc = pc[pc["zm"] == zm]
        if pc.empty: return None
        pc = pc[~pc["is_mtd"].astype(bool)]
        if pc.empty: return None
        t = A.rollup(pc, ["month", "oem"]).pivot(index="month", columns="oem", values="Ret %").sort_index()
        return ChartSpec("line", [month_label(m) for m in t.index], {OEM_LABEL.get(c, c): list(t[c]) for c in t.columns},
                         "Retention % by month - car brands (closed months)", pct=True)
    d = A.oem_frame(pack, oem, zm, rm)
    if d.empty: return None
    _, _, cur = A.months(d)
    d = d[~d["is_mtd"].astype(bool)]                      # closed months only - the running month is a partial base
    if d["month"].nunique() < 2: return None
    m = A.rollup(d, ["month"]).set_index("month").sort_index()
    series = {"Overall": list(m["Ret %"]), "First year": list(m["FY %"]), "Other year": list(m["OY %"])}
    if oem != "ROYAL ENFIELD":
        p = pc_pen_frame(pack, oem, zm, rm)
        if not p.empty:
            pm = p.groupby("month")[["new_nop", "retail"]].sum()
            series["Penetration"] = [ratio(pm.at[x, "new_nop"], pm.at[x, "retail"]) if x in pm.index else None for x in m.index]
    title = f"{PAGE_LABEL.get(oem, oem)} - retention{' and penetration' if 'Penetration' in series else ''} by month"
    if oem == "ROYAL ENFIELD" and not pack.get("re_history").empty and pack.get("re_dailymail").empty:
        title += " - North zone dealers (the monthly file covers Naman Singh's zone)"
    return ChartSpec("line", [month_label(x) + (" MTD" if x == cur else "") for x in m.index], series, title, pct=True,
                     target=TARGETS.get(oem, TARGETS["SKODA"])["TOT"])


def month_table(pack, oem, zm=None, rm=None) -> pd.DataFrame:
    """Month on month: first year, other year and overall retention (+ penetration for Audi / Skoda / VW)."""
    if oem == "OVERALL":
        # one row per month, retention % and penetration % for every OEM side by side
        r = pack.get("retention")
        if r.empty: return pd.DataFrame()
        if zm: r = r[r["zm"] == zm]
        out = None
        for o in ORDER:
            d = r[r["oem"] == o]
            if d.empty: continue
            g = A.rollup(d, ["month"]).set_index("month")
            col = pd.DataFrame({f"{OEM_LABEL[o]} retention %": g["Ret %"]})
            if o != "ROYAL ENFIELD":
                p = pc_pen_frame(pack, o, zm)
                if len(p):
                    pm = p.groupby("month")[["new_nop", "retail"]].sum()
                    col[f"{OEM_LABEL[o]} penetration %"] = (pm["new_nop"] / pm["retail"].where(pm["retail"] > 0)).reindex(col.index)
            out = col if out is None else out.join(col, how="outer")
        if out is None: return pd.DataFrame()
        out = out.sort_index()
        out = out[out.index >= f"{out.index.max()[:4]}-01"]
        cur = r[r["is_mtd"].astype(bool)]["month"].max() if r["is_mtd"].any() else None
        out.insert(0, "Month", [month_label(x) + (" (to date)" if x == cur else "") for x in out.index])
        return out.reset_index(drop=True)
    d = A.oem_frame(pack, oem, zm, rm)
    if d.empty: return pd.DataFrame()
    _, _, cur = A.months(d)
    m = A.rollup(d, ["month"]).sort_values("month")
    out = pd.DataFrame({"Month": [month_label(x) + (" (to date)" if x == cur else "") for x in m["month"]],
                        "FY due": m["FY base"], "FY renewed": m["FY ach"], "First year %": m["FY %"],
                        "OY due": m["OY base"], "OY renewed": m["OY ach"], "Other year %": m["OY %"],
                        "Total due": m["Base"], "Total renewed": m["Renewed"], "Overall %": m["Ret %"]})
    out["Change (pts)"] = out["Overall %"].diff() * 100
    if oem == "VOLVO":
        nt = volvo_ncb_table(pack, zm, rm)
        if len(nt):
            nt = nt.set_index(nt["Month"].str.replace(" (to date)", "", regex=False))
            key = out["Month"].str.replace(" (to date)", "", regex=False)
            for c in ("NCB %", "Non-NCB %"):
                if c in nt: out[c] = key.map(nt[c]).values
    if oem != "ROYAL ENFIELD":
        p = pc_pen_frame(pack, oem, zm, rm)
        if not p.empty:
            pm = p.groupby("month")[["new_nop", "retail"]].sum()
            out["Penetration %"] = [ratio(pm.at[x, "new_nop"], pm.at[x, "retail"]) if x in pm.index else None for x in m["month"]]
    return out


def scorecard(pack, oem, zm=None, rm=None, dealers=False) -> tuple[str, pd.DataFrame]:
    """One level below the scope (or every dealer in scope with dealers=True), retention and penetration side by side."""
    if oem == "OVERALL" and dealers:
        parts = []
        for o in ORDER:
            _, t = scorecard(pack, o, zm, rm, True)
            if len(t): parts.append(t.assign(OEM=OEM_LABEL[o]))
        return "Dealer", pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if oem == "OVERALL":
        rows = []
        for o in ORDER:
            s = summary(pack, o, zm); pn = pen_summary(pack, o, zm)
            y, m, l = s["ytd"], s["mtd"], s["lm"]
            if not (y["Base"] or m["Base"]): continue
            rows.append({"Name": OEM_LABEL[o], "Retention YTD": y["Ret %"] if y["Base"] else None,
                         "Last month": l["Ret %"] if l["Base"] else None, "MTD": m["Ret %"] if m["Base"] else None,
                         "First year YTD": y["FY %"] if y["Base"] else None, "Target": s["target"]["TOT"],
                         "Gap (pts)": (y["Ret %"] - s["target"]["TOT"]) * 100 if y["Base"] else None,
                         "Penetration YTD": pn["ytd"] if pn else None, "Penetration latest": pn["latest"] if pn else None,
                         "Due YTD": y["Base"], "RAG": A.rag(y["Ret %"] if y["Base"] else m["Ret %"], s["target"]["TOT"])})
        return "OEM", pd.DataFrame(rows)
    level, key = ("Dealer", "dealer_code") if dealers or rm else ("ZM", "zm") if not zm else ("RM", "rm")
    if oem == "ROYAL ENFIELD":
        t = re_group_table(pack, {"zm": "zm", "rm": "rm", "dealer_code": "dealer"}[key], zm)
        if t.empty: return level, t
        idc = {"zm": "ZM", "rm": "RM", "dealer_code": "Code"}[key]
        if rm and "RM" in t: t = t[t["RM"] == rm]
        pn = pen_by(pack, oem, key, zm, rm)
        if len(pn) and "Pen YTD %" in pn:
            t = t.set_index(idc).join(pn[["Pen YTD %", "Pen latest %"]], how="left").reset_index()
        else:                                   # no penetration rows for this scope
            t = t.assign(**{"Pen YTD %": None, "Pen latest %": None})
        conv = t["Conversion MTD %"] if "Conversion MTD %" in t else t["MTD FY %"]
        tgt = TARGETS["ROYAL ENFIELD"]["TOT"] if "Conversion MTD %" in t else TARGETS["ROYAL ENFIELD"]["FY"]
        out = pd.DataFrame({"Name": t["Dealer"] + " · " + t["Code"] if key == "dealer_code" else t[idc],
                            "Retention YTD": t.get("FYTD % (North)"),
                            "Last month": t.get("Same date last month %"),
                            "MTD": conv, "First year YTD": t.get("FYTD FY % (North)"),
                            "Target": tgt, "Gap (pts)": (conv - tgt) * 100,
                            "Penetration YTD": t["Pen YTD %"], "Penetration latest": t["Pen latest %"],
                            "Due YTD": t["Due so far"] if "Due so far" in t else t["MTD FY base"],
                            "NOP to target": ([max(0, math.ceil(tgt * d_ - (c_ * d_ if c_ == c_ else 0))) if d_ == d_ else 0
                                               for d_, c_ in zip(t["Due so far"], conv)] if "Due so far" in t else t["NOP to FY target"]),
                            "RAG": t["RAG"], "_id": t[idc]})
        out["RAG"] = [A.rag(v, tgt) for v in conv]
        out["MTD first year (whole month)"] = t["MTD FY %"].values
        out = out.rename(columns={"MTD": "Conversion MTD (due so far)", "Last month": "Same date last month",
                                  "Due YTD": "Due so far (MTD)" if "Due so far" in t else "Due MTD (FY)"})
        return level, out.sort_values("Gap (pts)")
    by = [key] + (["dealer_name"] if key == "dealer_code" else [])
    t = ytd_mtd_table(pack, oem, by, zm, rm)
    if t.empty: return level, t
    lm_col = next(c for c in t.columns if c.endswith(" %") and c not in ("YTD %", "FY YTD %", "OY YTD %", "Target %") and not _is_mtd(c))
    mtd_col = next(c for c in t.columns if _is_mtd(c))
    pn = pen_by(pack, oem, key, zm, rm)
    if len(pn): t = t.set_index(key).join(pn[["Pen YTD %", "Pen latest %", "Vehicles sold", "Policies"]], how="left").reset_index()
    for c in ("Pen YTD %", "Pen latest %", "Vehicles sold", "Policies"):
        if c not in t: t[c] = None
    out = pd.DataFrame({"Name": t["dealer_name"].astype(str) + " · " + t[key] if key == "dealer_code" else t[key],
                        "Retention YTD": t["YTD %"], "Last month": t[lm_col], "MTD": t[mtd_col], "First year YTD": t["FY YTD %"],
                        "Target": t["Target %"], "Gap (pts)": t["Gap (pts)"], "Penetration YTD": t["Pen YTD %"],
                        "Penetration latest": t["Pen latest %"], "Due YTD": t["YTD base"], "NOP to target": t["NOP to target"],
                        "RAG": t["RAG"], "_id": t[key], "_ren": t["YTD renewed"], "_sold": t["Vehicles sold"], "_pol": t["Policies"]})
    if key == "dealer_code":
        out["dealer_name"] = t["dealer_name"]; out["oem"] = oem
        out = G.tag(out, "dealer_name", "oem")
        out = G.club(out, "_id", ["Due YTD", "_ren", "NOP to target", "_sold", "_pol"],
                     {"Retention YTD": ("_ren", "Due YTD"), "Penetration YTD": ("_pol", "_sold")}, "Name",
                     sort_col="Gap (pts)", ascending=True)
        tot = out["Level"] == "Group"
        if tot.any():
            tg = TARGETS.get(oem, TARGETS["SKODA"])["TOT"]
            out.loc[tot, "Gap (pts)"] = (out.loc[tot, "Retention YTD"] - tg) * 100
            out.loc[tot, "RAG"] = [A.rag(v, tg) for v in out.loc[tot, "Retention YTD"]]
            for c in ("Last month", "MTD", "First year YTD", "Penetration latest"):
                out.loc[tot, c] = None
        out = out.drop(columns=["dealer_name", "oem", "group"], errors="ignore")
    else:
        out = out.sort_values("Gap (pts)")
    return level, out.drop(columns=["_ren", "_sold", "_pol"], errors="ignore")


def messages(pack, oem, zm, rm, level, sc: pd.DataFrame) -> list[str]:
    """At most three short, numeric messages."""
    out = []
    if sc.empty: return out
    d = sc[sc.get("Level", pd.Series("Dealer", index=sc.index)).isin(["Dealer", "Group"])] if "Level" in sc else sc
    d = d.dropna(subset=["Gap (pts)"]) if "Gap (pts)" in d else d
    if oem == "OVERALL":
        below = d[d["Gap (pts)"] < 0]
        if len(below): out.append(f"{len(below)} of {len(d)} OEMs are below target YTD; widest gap **{below.iloc[below['Gap (pts)'].argmin()]['Name']}** "
                                  f"({below['Gap (pts)'].min():+.0f} pts).")
        if d["Last month"].notna().any():
            lm_best = d.dropna(subset=["Last month"]).sort_values("Last month").iloc[-1]
            out.append(f"Best last closed month: **{lm_best['Name']}** at {A.pct(lm_best['Last month'])}; the running month is "
                       "shown separately as it is not closed yet.")
        pen = d.dropna(subset=["Penetration YTD"])
        if len(pen): out.append("Penetration YTD: " + ", ".join(f"{r['Name']} {r['Penetration YTD'] * 100:.0f}%" for _, r in pen.iterrows()) + ".")
        return out[:3]
    if len(d):
        worst = d.sort_values("Gap (pts)").iloc[0]
        red = int((d["RAG"] == "Red").sum())
        out.append(f"{red} of {len(d)} {level}s are Red; the largest gap is **{worst['Name']}** at {worst['Gap (pts)']:+.0f} pts to target.")
        if "NOP to target" in d and d["NOP to target"].notna().any():
            top = d.sort_values("NOP to target", ascending=False).head(3)
            share = top["NOP to target"].sum() / max(1, d["NOP to target"].sum())
            out.append(f"The top 3 {level}s hold {share:.0%} of the renewals still needed ({A.n(top['NOP to target'].sum())} of {A.n(d['NOP to target'].sum())}).")
        pen = d.dropna(subset=["Penetration YTD"])
        if len(pen) >= 3:
            lo = pen.sort_values("Penetration YTD").iloc[0]
            out.append(f"Lowest penetration: **{lo['Name']}** at {A.pct(lo['Penetration YTD'])} YTD (scope average "
                       f"{A.pct(pen['Penetration YTD'].median())}).")
    return out[:3]


def overview(pack, oem, zm=None, rm=None) -> dict:
    level, sc = scorecard(pack, oem, zm, rm)
    scope = " · ".join(x for x in (PAGE_LABEL.get(oem, oem), (zm or "").title(), (rm or "").title()) if x)
    d = sc[sc["Level"].isin(["Dealer", "Group"])] if "Level" in sc else sc
    att = best = pd.DataFrame()
    if len(d) and "Gap (pts)" in d:
        x = d.dropna(subset=["Gap (pts)"])
        if "Due YTD" in x: x = x[x["Due YTD"].fillna(0) >= A.MIN_BASE] if level == "Dealer" else x
        n = min(5, len(x) // 2)              # never list the same name in both boxes
        att = x.sort_values("Gap (pts)").head(n)
        best = x.sort_values("Gap (pts)", ascending=False).head(n)
    asof = A._asof_line(pack, "Royal Enfield" if oem == "ROYAL ENFIELD" else None if oem == "OVERALL" else "Private car")
    return {"title": f"Performance overview - {scope}", "scope": scope, "asof": asof, "level": level,
            "kpis": kpis(pack, oem, zm, rm), "trend": trend(pack, oem, zm, rm), "score": sc, "months": month_table(pack, oem, zm, rm),
            "follow_up": follow_up_report(pack, oem, zm, rm) if (zm or rm) else None,
            "attention": att, "best": best, "messages": messages(pack, oem, zm, rm, level, sc),
            "note": (summary(pack, "ROYAL ENFIELD", zm, rm)["note"] + " Penetration = program policies / bikes sold."
                     if oem == "ROYAL ENFIELD" else
                     "Volvo has no penetration data in the MIS. The running month is to date: its base is the whole month's "
                     "due list, so it is not comparable with closed months." if oem == "VOLVO" else
                     "Penetration = new policies / cars sold (SAVWIPL MIS). The running month is to date: its base is the "
                     "whole month's due list, so compare it with the same point last month, not with closed months." if oem != "OVERALL" else
                     "Retention YTD: cars Jan to date; Royal Enfield April to date (North). Penetration and retention are separate books.")}


PCT_COLS = {"Retention YTD", "Last month", "MTD", "First year YTD", "Target", "Target %", "Penetration YTD", "Penetration latest",
            "Conversion MTD (due so far)", "Same date last month", "MTD first year (whole month)", "Retention"}


def _fmt_export(df: pd.DataFrame) -> pd.DataFrame:
    """Percent columns as '45.2%' and gaps as '+3.1' so PDF / PPT / HTML never show raw fractions."""
    d = df.copy()
    for c in d.columns:
        if c in PCT_COLS:
            d[c] = [A.pct(x) if isinstance(x, (int, float)) and x == x else "" for x in pd.to_numeric(d[c], errors="coerce")]
        elif c == "Gap (pts)":
            d[c] = [f"{x:+.1f}" if x == x else "" for x in pd.to_numeric(d[c], errors="coerce")]
        elif c == "Level":
            d = d.drop(columns=[c])
    return d


def to_report(v: dict) -> Report:
    rep = Report(v["title"], v["asof"])
    tiles = []
    for c in v["kpis"]:
        val = c["text"] if c.get("text") is not None else f"{c['count']:,}" if c.get("count") is not None else A.pct(c["value"])
        sub = " · ".join(f"{k} {A.pct(x)}" for k, x in c.get("lines", []) if x is not None)
        if c.get("pen"): sub += f" · {c['pen'][0]} {A.pct(c['pen'][1])}"
        tiles.append((f"{c['title']} - {c['per']}", val, sub or c.get("sub", "")))
    rep.kpis(tiles)
    if v["messages"]: rep.bullets(v["messages"], "Key messages")
    ix = v.get("ix")
    if ix:
        if ix["tiles"]: rep.kpis([(t, val, sub) for t, val, sub, _ in ix["tiles"]], "Executive cockpit")
        if ix["so_what"]: rep.bullets([ix["so_what"]], "So what")
        if ix["chart"] is not None: rep.chart(ix["chart"])
        if ix["calendar"]: rep.bullets([x.replace("**", "") for x in ix["calendar"]], "Calendar - next 60 days")
        rep.bullets([x.replace("**", "") for x in ix["playbook"]], "Industry playbook")
    tp = v.get("tp")
    if tp:
        import re as _re
        t_ = lambda h: _re.sub("<[^>]+>", "", str(h))
        if tp.get("headline"): rep.bullets([t_(tp["headline"])], "Headline")
        for c in tp.get("cards", []):
            rep.bullets([" · ".join(t_(x) for x in c["stats"])] + [t_(x) for x in c["points"]]
                        + ([f"Ask: {c['ask']}"] if c.get("ask") else []), f"{c['title']} ({ {'r':'red','a':'amber','g':'green'}.get(c['status'], '') })")
        if tp.get("flags"): rep.bullets([t_(x) for x in tp["flags"]], "Red flags")
        if tp.get("wins"): rep.bullets([t_(x) for x in tp["wins"]], "Wins")
        if tp.get("bars") is not None: rep.chart(tp["bars"])
    if v["trend"] is not None: rep.chart(v["trend"])
    if len(v.get("months", [])): rep.table(v["months"], "Month on month - first year, other year, overall")
    sc = _fmt_export(v["score"].drop(columns=[c for c in v["score"].columns if c.startswith("_")], errors="ignore"))
    if len(sc): rep.table(sc, f"{v['level']} scorecard - retention and penetration")
    for k, lab in (("attention", "Needs attention"), ("best", "Best performers")):
        if len(v[k]): rep.table(_fmt_export(v[k][[c for c in ("Name", "Retention YTD", "Gap (pts)", "Penetration YTD", "NOP to target") if c in v[k]]]), lab)
    rep.bullets([v["note"]])
    if v.get("follow_up") is not None:
        rep.extend(Report("", "", v["follow_up"].blocks))
    return rep


# --------------------------------------------------------------------------- #
# penetration report and group report
# --------------------------------------------------------------------------- #
def penetration_page(pack, oem, zm=None, rm=None) -> Report:
    scope = " · ".join(x for x in (PAGE_LABEL.get(oem, oem), (zm or "").title(), (rm or "").title()) if x)
    rep = Report(f"Penetration - {scope}", "Policies sold with the vehicle / vehicles sold. Clubbed groups: codes first, then the group total.")
    oems = list(ORDER) if oem == "OVERALL" else [oem]
    if oem == "VOLVO" and pc_pen_frame(pack, "VOLVO", zm, rm).empty:
        sn = volvo_safenet(pack, zm, rm)
        if not sn:
            return rep.p("The MIS drop has no Volvo penetration data (the SAVWIPL penetration MIS covers Audi, Skoda and VW).")
        rep.kpis([("Safenet share of renewals", A.pct(sn["share"]), f"policies renewed {sn['renewed_label']}"),
                  ("Renewed through Safenet", A.n(sn["safenet"]), ""), ("Renewed outside Safenet", A.n(sn["non_safenet"]), ""),
                  (sn["cohort_label"] or "Policies in the cohort", A.n(sn["cohort"]), "")])
        sf = pack.get("volvo_safenet")
        codes = _volvo_scope_codes(pack, zm, rm)
        if codes is not None: sf = sf[sf["dealer_code"].astype(str).str.upper().isin(codes)]
        names = pack.get("volvo_roster").set_index("dealer_code")["dealer_name"] if len(pack.get("volvo_roster")) else {}
        t = sf.assign(Dealer=sf["dealer_code"].map(names), **{"Safenet share": [ratio(a, a + b) if a + b else None for a, b in zip(sf["safenet"], sf["non_safenet"])]})
        rep.table(t[["dealer_code", "Dealer", "cohort_policies", "safenet", "non_safenet", "Safenet share"]].rename(
            columns={"dealer_code": "Code", "cohort_policies": "Policies in cohort", "safenet": "Renewed via Safenet", "non_safenet": "Renewed outside Safenet"}),
            "By dealer")
        rep.bullets(["Volvo files carry no car-sales (retail) figures, so new-car penetration cannot be calculated. Shown instead: "
                     "the share of renewals made through the Safenet programme. Add a Volvo retail / sales file to Drive for true penetration."])
        return rep
    tiles = []
    for o in oems:
        s = pen_summary(pack, o, zm, rm)
        if s: tiles.append((f"{OEM_LABEL[o]} - {s['ytd_label']}", A.pct(s["ytd"]), f"{s['latest_label']} {A.pct(s['latest'])} · "
                            f"{A.n(s['policies'])} / {A.n(s['sold'])}"))
    rep.kpis(tiles)
    for o in oems:
        if len(oems) > 1: rep.h(OEM_LABEL[o])
        if o == "ROYAL ENFIELD":
            p = re_pen_frame(pack, zm, rm)
            if p.empty: continue
            key = "rm" if zm else "zm"
            p = p.assign(zm=[re_zm(x) for x in p["region"]])
            g = p.groupby(key).agg(**{"Bikes YTD": ("veh_ytd", "sum"), "Policies YTD": ("prog_ytd", "sum"),
                                      "Bikes MTD": ("veh_mtd", "sum"), "Policies MTD": ("prog_mtd", "sum")}).reset_index()
            g["Pen YTD %"] = g["Policies YTD"] / g["Bikes YTD"].where(g["Bikes YTD"] > 0)
            g["Pen MTD %"] = g["Policies MTD"] / g["Bikes MTD"].where(g["Bikes MTD"] > 0)
            if "addon_ytd" in p:
                ad = p.groupby(key)[["addon_ytd", "addon_mtd"]].sum()
                g["Add-on YTD %"] = g[key].map(ad["addon_ytd"]) / g["Policies YTD"].where(g["Policies YTD"] > 0)
                g["Add-on MTD %"] = g[key].map(ad["addon_mtd"]) / g["Policies MTD"].where(g["Policies MTD"] > 0)
            g = g.sort_values("Pen YTD %")
            rep.chart(ChartSpec("hbar", list(g[key]), {"Penetration YTD": list(g["Pen YTD %"])}, f"Penetration YTD by {key.upper()}", pct=True))
            rep.table(g.rename(columns={"zm": "ZM", "rm": "RM"}), f"By {key.upper()}")
            mix_cols = [c for c in ("essential", "advance", "premium", "bundled", "aloa", "no_addon") if c in p]
            if mix_cols and p[mix_cols].sum().sum():
                tot = p[mix_cols].sum()
                mix = pd.DataFrame({"Cover": [c.replace("_", " ").replace("aloa", "ALOA").title().replace("Aloa", "ALOA") for c in mix_cols],
                                    "Policies": list(tot.values), "Share": list((tot / tot.sum()).values)})
                rep.chart(ChartSpec("bar", list(mix["Cover"]), {"Share of policies": list(mix["Share"])}, "Add-on cover mix", pct=True))
                rep.table(mix, "Add-on cover mix (as in the penetration report)")
            low = p[p["veh_mtd"] >= 20].assign(**{"Pen MTD %": lambda x: x["prog_mtd"] / x["veh_mtd"],
                                                  "Pen YTD %": lambda x: x["prog_ytd"] / x["veh_ytd"].where(x["veh_ytd"] > 0)})
            if "addon_ytd" in low:
                low = low.assign(**{"Add-on YTD %": lambda x: x["addon_ytd"] / x["prog_ytd"].where(x["prog_ytd"] > 0)})
            rep.table(low.sort_values("Pen MTD %").head(15)[["dealer_code", "dealer_name", "rm", "veh_mtd", "prog_mtd", "Pen MTD %", "Pen YTD %"]
                                                            + (["Add-on YTD %"] if "Add-on YTD %" in low else [])]
                      .rename(columns={"dealer_code": "Code", "dealer_name": "Dealer", "rm": "RM", "veh_mtd": "Bikes MTD", "prog_mtd": "Policies MTD"}),
                      "Lowest penetration this month (20+ bikes)")
            continue
        p = pc_pen_frame(pack, o, zm, rm)
        if p.empty: continue
        m = p.groupby("month")[["retail", "new_nop"]].sum()
        rep.chart(ChartSpec("line", [month_label(x) for x in m.index], {"Penetration": list(m["new_nop"] / m["retail"].where(m["retail"] > 0))},
                            f"{OEM_LABEL[o]} - penetration by month", pct=True))
        key = "rm" if zm else "zm"
        g = pen_by(pack, o, key, zm, rm).reset_index().sort_values("Pen YTD %")
        rep.table(g.drop(columns=["_sl", "_pl"]).rename(columns={"zm": "ZM", "rm": "RM"}), f"By {key.upper()} - YTD and latest month")
        d = pen_by(pack, o, "dealer_code", zm, rm).reset_index()
        names = p.assign(dealer_code=p["dealer_code"].map(G.parent_code)).drop_duplicates("dealer_code").set_index("dealer_code")["dealer_name"]
        d["Dealer"] = d["dealer_code"].map(names).fillna("")
        d["oem"] = o
        d = G.tag(d, "Dealer", "oem")
        d = d[d["Vehicles sold"] > 0]
        d = G.club(d.rename(columns={"dealer_code": "Code"}), "Code", ["Vehicles sold", "Policies", "_sl", "_pl"],
                   {"Pen YTD %": ("Policies", "Vehicles sold"), "Pen latest %": ("_pl", "_sl")}, "Dealer",
                   sort_col="Pen YTD %", ascending=True)
        rep.table(d[["Level", "Code", "Dealer", "Vehicles sold", "Policies", "Pen YTD %", "Pen latest %"]],
                  "Dealers, lowest penetration first (groups: codes, then total)")
    rep.bullets(["Penetration and retention are separate books: a policy sold with a new vehicle is not a renewal.",
                 "Over 100% is possible - policies issued in a month against vehicles sold earlier."])
    return rep


def group_report(pack, oem=None, zm=None) -> Report:
    """Every clubbed dealer group: each code's retention and penetration, then the group total."""
    rep = Report("Dealer groups (clubbed dealers)" + (f" - {OEM_LABEL.get(oem, oem)}" if oem and oem != "OVERALL" else ""),
                 "Each code line by line, then the group total. Rules: " + ", ".join(g for g, _ in G.GROUP_RULES))
    found = []
    for o in [x for x in ("AUDI", "SKODA", "VOLKSWAGEN") if oem in (None, "OVERALL", x)]:
        t = ytd_mtd_table(pack, o, ["dealer_code", "dealer_name", "rm", "zm"], zm)
        pn = pen_by(pack, o, "dealer_code", zm)
        if t.empty: continue
        t = t.set_index("dealer_code").join(pn[["Vehicles sold", "Policies", "Pen YTD %"]], how="outer").reset_index()
        pnames = pc_pen_frame(pack, o, zm)
        if len(pnames):
            nm = pnames.assign(dealer_code=pnames["dealer_code"].map(G.parent_code)).drop_duplicates("dealer_code").set_index("dealer_code")["dealer_name"]
            t["dealer_name"] = t["dealer_name"].fillna(t["dealer_code"].map(nm))
        t["oem"] = o
        t = G.tag(t, "dealer_name", "oem")
        t = t[t["group"].notna()]
        if t.empty: continue
        for c in ("YTD base", "YTD renewed", "Vehicles sold", "Policies", "NOP to target"):
            t[c] = pd.to_numeric(t[c], errors="coerce").fillna(0)
        rep.h(OEM_LABEL[o])
        c = G.club(t.rename(columns={"dealer_code": "Code", "dealer_name": "Dealer", "rm": "RM", "zm": "ZM"}), "Code",
                   ["YTD base", "YTD renewed", "Vehicles sold", "Policies", "NOP to target"],
                   {"YTD %": ("YTD renewed", "YTD base"), "Pen YTD %": ("Policies", "Vehicles sold")}, "Dealer", sort_col="group")
        cols = [x for x in ("Level", "group", "Code", "Dealer", "RM", "YTD base", "YTD renewed", "YTD %", "FY YTD %",
                            next((k for k in c.columns if _is_mtd(k)), None), "Vehicles sold", "Policies", "Pen YTD %",
                            "NOP to target", "RAG") if x and x in c]
        rep.table(c[cols].rename(columns={"group": "Group"}))
        found += sorted(set(t["group"]))
    missing = sorted({g for g, _ in G.GROUP_RULES} - set(found))
    if missing: rep.bullets([f"No codes in this scope for: {', '.join(missing)}."])
    return rep


def zm_review(pack, zm, oem="OVERALL") -> Report:
    rep = Report(f"ZM review - {zm.title()}", "Every OEM in the zone: retention, penetration, RMs and dealers")
    for o in (ORDER if oem == "OVERALL" else [oem]):
        v = overview(pack, o, zm)
        if v["score"].empty: continue
        sub = to_report(v)
        rep.h(OEM_LABEL[o]); rep.extend(Report("", "", sub.blocks))
    return rep


def ret_pen_matrix(pack, oem, zm=None, rm=None) -> Report:
    """Dealers placed on retention YTD x penetration YTD, four quadrants with the action for each."""
    rep = Report(f"Retention x penetration - {PAGE_LABEL.get(oem, oem)}", "Where each dealer sits on both key parameters")
    rows = []
    for o in ([x for x in ("AUDI", "SKODA", "VOLKSWAGEN")] if oem in ("OVERALL", None) else [oem]):
        if o in ("VOLVO", "ROYAL ENFIELD"): continue
        t = ytd_mtd_table(pack, o, ["dealer_code", "dealer_name", "rm"], zm, rm)
        pn = pen_by(pack, o, "dealer_code", zm, rm)
        if t.empty or pn.empty: continue
        t = t.set_index("dealer_code").join(pn[["Pen YTD %", "Vehicles sold"]], how="inner").reset_index()
        t["OEM"] = OEM_LABEL[o]
        rows.append(t[(t["YTD base"] >= A.MIN_BASE) & (t["Vehicles sold"] >= 10)])
    if not rows: return rep.p("Needs retention and penetration for the same dealers (Audi / Skoda / VW).")
    t = pd.concat(rows)
    rm_ = t["YTD %"].median(); pm = t["Pen YTD %"].median()
    def quad(r):
        hi_r, hi_p = r["YTD %"] >= rm_, r["Pen YTD %"] >= pm
        return ("Stars - protect" if hi_r and hi_p else "Retains but under-sells - fix point of sale" if hi_r else
                "Sells but loses at renewal - fix renewal calling" if hi_p else "Weak on both - joint plan")
    t["Quadrant"] = t.apply(quad, axis=1)
    q = t.groupby("Quadrant").agg(Dealers=("dealer_code", "size"), **{"Due YTD": ("YTD base", "sum")}).reset_index()
    rep.kpis([(r.Quadrant.split(" - ")[0], A.n(r.Dealers), r.Quadrant.split(" - ")[-1]) for r in q.itertuples()],
             f"Split at the medians: retention {A.pct(rm_)}, penetration {A.pct(pm)}")
    rep.chart(ChartSpec("scatter", list(t["Pen YTD %"]), {"Retention YTD": list(t["YTD %"])}, "Dealers: penetration (x) vs retention (y)", pct=True))
    rep.table(t.sort_values(["Quadrant", "YTD %"])[["Quadrant", "OEM", "dealer_code", "dealer_name", "rm", "YTD %", "Pen YTD %", "YTD base", "Vehicles sold"]]
              .rename(columns={"dealer_code": "Code", "dealer_name": "Dealer", "rm": "RM", "YTD %": "Retention YTD", "Pen YTD %": "Penetration YTD"}))
    return rep


# --------------------------------------------------------------------------- #
# geography: region (RE) / branch (cars) / state, with dealer lists
# --------------------------------------------------------------------------- #
SHOW = {"all": "All dealers", "critical": "Critical dealers", "performing": "Performing dealers"}


def dealer_geo(pack, oem, zm=None, rm=None) -> pd.DataFrame:
    """One row per dealer with region / state, the retention measure, target, gap, penetration and RAG."""
    rows = []
    for o in (ORDER if oem == "OVERALL" else [oem]):
        tg = TARGETS.get(o, TARGETS["SKODA"])
        if o == "ROYAL ENFIELD":
            s = pack.get("re_dealer_snapshot")
            if s.empty: continue
            if zm: s = s[s["zm"] == zm]
            if rm: s = s[s["rm"] == rm]
            pn = pen_by(pack, o, "dealer_code", zm, rm)
            for x in s.itertuples():
                r = ratio(x.fy_ach, x.fy_base)
                rows.append({"OEM": "Royal Enfield", "Code": x.dealer_code, "Dealer": x.dealer_name, "RM": x.rm, "ZM": x.zm,
                             "Region": x.region, "State": str(x.state).upper(), "Measure": "First year MTD",
                             "Retention": r, "Target": tg["FY"], "Gap (pts)": (r - tg["FY"]) * 100 if r == r else None,
                             "Due": x.fy_base, "Renewed": x.fy_ach,
                             "To target": max(0, math.ceil(tg["FY"] * x.fy_base - x.fy_ach)) if x.fy_base else 0,
                             "Penetration YTD": pn["Pen YTD %"].get(x.dealer_code) if len(pn) else None})
            continue
        t = ytd_mtd_table(pack, o, ["dealer_code", "dealer_name", "rm", "zm"], zm, rm)
        if t.empty: continue
        r_ = pack.get("retention")
        br = r_[r_["oem"] == o].drop_duplicates("dealer_code").set_index("dealer_code")["branch"]
        pp = pc_pen_frame(pack, o)
        st_ = (pp.assign(dealer_code=pp["dealer_code"].map(G.parent_code)).drop_duplicates("dealer_code")
               .set_index("dealer_code")["state"]) if len(pp) else pd.Series(dtype=str)
        pn = pen_by(pack, o, "dealer_code", zm, rm)
        rows += pd.DataFrame({"OEM": OEM_LABEL[o], "Code": t["dealer_code"], "Dealer": t["dealer_name"], "RM": t["rm"], "ZM": t["zm"],
                              "Region": [str(br.get(c, "") or "").upper() or "-" for c in t["dealer_code"]],
                              "State": [str(st_.get(c, "") or "").upper() or "-" for c in t["dealer_code"]],
                              "Measure": "Retention YTD", "Retention": t["YTD %"], "Target": tg["TOT"], "Gap (pts)": t["Gap (pts)"],
                              "Due": t["YTD base"], "Renewed": t["YTD renewed"], "To target": t["NOP to target"],
                              "Penetration YTD": [pn["Pen YTD %"].get(c) if len(pn) else None for c in t["dealer_code"]]}).to_dict("records")
    df = pd.DataFrame(rows)
    if df.empty: return df
    df["RAG"] = [A.rag(r, t) for r, t in zip(df["Retention"], df["Target"])]
    df["Group"] = [G.group_of(n, o.upper()) or "" for n, o in zip(df["Dealer"], df["OEM"].map(lambda v: {lv: k for k, lv in OEM_LABEL.items()}.get(v, v)))]
    return df


def geo_report(pack, oem, by="State", zm=None, rm=None, area=None, show="all") -> Report:
    """Area summary (retention + penetration) and the dealers of each area, ranked best to worst.
    show = all | critical (Red, base >= minimum) | performing (at or above target)."""
    col = "Region" if by.lower().startswith("region") else "State"
    scope = " · ".join(x for x in (PAGE_LABEL.get(oem, oem), (zm or "").title(), (rm or "").title()) if x)
    lab = "Region" if col == "Region" and oem == "ROYAL ENFIELD" else "Region / branch" if col == "Region" else "State"
    rep = Report(f"{lab}-wise report - {scope}" + (f" - {area}" if area else ""),
                 f"{SHOW.get(show, 'All dealers')}, ranked by performance. Retention: cars YTD, Royal Enfield first year MTD.")
    d = dealer_geo(pack, oem, zm, rm)
    if d.empty: return rep.p("No dealers in this scope.")
    if area: d = d[d[col] == area]
    g = d.groupby(col).agg(Dealers=("Code", "size"), Due=("Due", "sum"), Renewed=("Renewed", "sum"),
                           Red=("RAG", lambda s: int((s == "Red").sum())), **{"To target": ("To target", "sum")}).reset_index()
    g["Retention"] = g["Renewed"] / g["Due"].where(g["Due"] > 0)
    pen = d.dropna(subset=["Penetration YTD"]).groupby(col)["Penetration YTD"].median()
    g["Penetration (median dealer)"] = g[col].map(pen)
    # RE regions: national region figures (FY / OY / total, vs same date last month) from the region view
    if oem == "ROYAL ENFIELD" and col == "Region":
        rg = pack.get("re_region")
        if not rg.empty:
            rg = rg.set_index("region")
            g["State(s)"] = g[col].map(rg["state"])
            g["FY MTD"] = g[col].map(rg["fy_ach"] / rg["fy_base"].where(rg["fy_base"] > 0))
            g["FY same date last month"] = g[col].map(rg["fy_ach_prev"] / rg["fy_base_prev"].where(rg["fy_base_prev"] > 0))
            g["OY MTD"] = g[col].map(rg["oy_ach"] / rg["oy_base"].where(rg["oy_base"] > 0))
            g["Overall MTD"] = g[col].map(rg["tot_ach"] / rg["tot_base"].where(rg["tot_base"] > 0))
    g = g.sort_values("Retention")
    rep.kpis([(f"{lab}s", A.n(len(g)), ""), ("Dealers", A.n(len(d)), f"{int((d['RAG'] == 'Red').sum())} Red"),
              ("Retention", A.pct(ratio(d["Renewed"].sum(), d["Due"].sum())), f"{A.n(d['Renewed'].sum())} / {A.n(d['Due'].sum())}"),
              ("Renewals to target", A.n(d["To target"].sum()), "")])
    if len(g) > 1:
        rep.chart(ChartSpec("hbar", list(g[col].astype(str)), {"Retention": list(g["Retention"])}, f"Retention by {lab.lower()}",
                            pct=True, target=float(d["Target"].median())))
    rep.table(g, f"By {lab.lower()} - weakest first")
    sel = d.copy()
    if show == "critical": sel = sel[(sel["RAG"] == "Red") & (sel["Due"] >= A.MIN_BASE)]
    elif show == "performing": sel = sel[sel["Retention"] >= sel["Target"]]
    sel = sel.sort_values(["Retention", "Due"], ascending=[False, False])
    cols = ["OEM", "Code", "Dealer", "Group", "RM", "Measure", "Retention", "Target", "Gap (pts)", "Due", "Renewed", "To target",
            "Penetration YTD", "RAG"]
    if oem != "OVERALL": cols.remove("OEM")
    if not (sel["Group"] != "").any(): cols.remove("Group")
    order = list(g.sort_values("Retention", ascending=False)[col])
    for a in order:
        x = sel[sel[col] == a]
        if x.empty: continue
        rep.h(f"{a} - {len(x)} {SHOW.get(show, 'dealers').lower()}")
        rep.table(x[cols].reset_index(drop=True))
    if sel.empty: rep.p(f"No {SHOW.get(show, '').lower()} in this scope.")
    return rep


# --------------------------------------------------------------------------- #
# one clubbed group: combined dashboard
# --------------------------------------------------------------------------- #
def group_list(pack, oem=None, zm=None) -> list[tuple[str, str]]:
    """[(oem, group)] for every clubbed group with at least one code in scope."""
    r = pack.get("retention")
    r = r[r["oem"].isin(["AUDI", "SKODA", "VOLKSWAGEN", "PORSCHE"] if oem in (None, "OVERALL") else [oem])]
    if zm: r = r[r["zm"] == zm]
    out = set()
    for o, n in r.drop_duplicates(["oem", "dealer_code"])[["oem", "dealer_name"]].itertuples(index=False):
        g = G.group_of(n, o)
        if g: out.add((o, g))
    p = pack.get("pc_penetration_month")
    if len(p):
        p = p[p["oem"].isin(["AUDI", "SKODA", "VOLKSWAGEN", "PORSCHE"] if oem in (None, "OVERALL") else [oem])]
        if zm: p = p[p["zm"] == zm]
        for o, n in p.drop_duplicates(["oem", "dealer_code"])[["oem", "dealer_name"]].itertuples(index=False):
            g = G.group_of(n, o)
            if g: out.add((o, g))
    return sorted(out, key=lambda x: (x[0], x[1]))


def group_codes(pack, oem, group) -> list[str]:
    codes = set()
    r = pack.get("retention")
    for c, n in r[r["oem"] == oem].drop_duplicates("dealer_code")[["dealer_code", "dealer_name"]].itertuples(index=False):
        if G.group_of(n, oem) == group: codes.add(c)
    p = pack.get("pc_penetration_month")
    if len(p):
        for c, n in p[p["oem"] == oem].drop_duplicates("dealer_code")[["dealer_code", "dealer_name"]].itertuples(index=False):
            if G.group_of(n, oem) == group: codes.add(G.parent_code(c))
    return sorted(codes)


def group_dashboard(pack, oem, group) -> Report:
    """Combined view of one dealer group: totals first (YTD / last month / MTD, FY / OY, penetration, renewals to
    target), the month-on-month trend, then every code line by line with the group total, RMs and pending payouts."""
    from .views import _agg
    codes = group_codes(pack, oem, group)
    t = TARGETS.get(oem, TARGETS["SKODA"])
    rep = Report(f"{group} group - {OEM_LABEL.get(oem, oem)} combined dashboard",
                 f"{len(codes)} codes: {', '.join(codes)} · " + A._asof_line(pack, "Private car"))
    if not codes: return rep.p("No codes found for this group.")
    d = pack.get("retention")
    d = d[(d["oem"] == oem) & d["dealer_code"].isin(codes)]
    f = period_frames(d) if len(d) else None
    pp = pc_pen_frame(pack, oem)
    pp = pp[pp["dealer_code"].map(G.parent_code).isin(codes)] if len(pp) else pp
    tiles = []
    if f is not None:
        y, l, m = _agg(f["ytd"]), _agg(f["lm"]), _agg(f["mtd"])
        tiles += [(f"Retention {f['ytd_label']}", A.pct(y["Ret %"]), f"{A.n(y['Renewed'])} / {A.n(y['Base'])} · target {A.pct(t['TOT'])}"),
                  ("First year YTD", A.pct(y["FY %"]), f"target {A.pct(t['FY'])}"),
                  ("Other year YTD", A.pct(y["OY %"]), f"target {A.pct(t['OY'])}"),
                  (f"{f['lm_label']}", A.pct(l["Ret %"]), f"FY {A.pct(l['FY %'])} · OY {A.pct(l['OY %'])}"),
                  (f"{f['mtd_label']}", A.pct(m["Ret %"]), f"{A.n(m['Renewed'])} / {A.n(m['Base'])} due so far"),
                  ("Renewals to target (YTD)", A.n(max(0, math.ceil(t["TOT"] * y["Base"] - y["Renewed"]))), "")]
    if len(pp):
        last = pp["month"].max(); yy = pp[pp["month"].str[:4] == last[:4]]; ll = pp[pp["month"] == last]
        tiles.append((f"Penetration YTD (to {month_label(last)})", A.pct(ratio(yy["new_nop"].sum(), yy["retail"].sum())),
                      f"{month_label(last)} {A.pct(ratio(ll['new_nop'].sum(), ll['retail'].sum()))} · {A.n(yy['new_nop'].sum())} / {A.n(yy['retail'].sum())} cars"))
    rep.kpis(tiles, "Group total")
    if len(d):
        mm = A.rollup(d, ["month"]).sort_values("month")
        _, _, cur = A.months(d)
        lab = [month_label(x) + (" MTD" if x == cur else "") for x in mm["month"]]
        series = {"Overall": list(mm["Ret %"]), "First year": list(mm["FY %"]), "Other year": list(mm["OY %"])}
        if len(pp):
            pm = pp.groupby("month")[["new_nop", "retail"]].sum()
            series["Penetration"] = [ratio(pm.at[x, "new_nop"], pm.at[x, "retail"]) if x in pm.index else None for x in mm["month"]]
        rep.chart(ChartSpec("line", lab, series, f"{group} group - month on month", pct=True, target=t["TOT"]))
        tb = pd.DataFrame({"Month": lab, "FY due": mm["FY base"], "FY renewed": mm["FY ach"], "First year %": mm["FY %"],
                           "OY due": mm["OY base"], "OY renewed": mm["OY ach"], "Other year %": mm["OY %"],
                           "Total due": mm["Base"], "Total renewed": mm["Renewed"], "Overall %": mm["Ret %"]})
        if "Penetration" in series: tb["Penetration %"] = series["Penetration"]
        rep.table(tb, "Month on month - group total")
    # line items then total
    yt = ytd_mtd_table(pack, oem, ["dealer_code", "dealer_name", "rm", "zm"])
    yt = yt[yt["dealer_code"].isin(codes)] if len(yt) else yt
    pn = pen_by(pack, oem, "dealer_code")
    li = pd.DataFrame({"dealer_code": codes}).merge(yt, on="dealer_code", how="left")
    if len(pn): li = li.join(pn[["Vehicles sold", "Policies", "Pen YTD %"]], on="dealer_code")
    mtd_col = next((c for c in li.columns if _is_mtd(c)), None)
    lm_col = next((c for c in li.columns if c.endswith(" %") and c not in ("YTD %", "FY YTD %", "OY YTD %", "Target %", "Pen YTD %")
                   and not _is_mtd(c)), None)
    rows = []
    for x in li.itertuples(index=False):
        x = dict(zip(li.columns, x))
        rows.append({"Code": x["dealer_code"], "Dealer": x.get("dealer_name") or "", "RM": x.get("rm") or "", "ZM": x.get("zm") or "",
                     "Due YTD": x.get("YTD base"), "Renewed YTD": x.get("YTD renewed"), "Retention YTD": x.get("YTD %"),
                     "First year YTD": x.get("FY YTD %"), "Other year YTD": x.get("OY YTD %"),
                     "Last month": x.get(lm_col) if lm_col else None, "MTD": x.get(mtd_col) if mtd_col else None,
                     "Cars sold YTD": x.get("Vehicles sold"), "Policies YTD": x.get("Policies"), "Penetration YTD": x.get("Pen YTD %"),
                     "To target": x.get("NOP to target")})
    lt = pd.DataFrame(rows)
    num = lambda c: pd.to_numeric(lt[c], errors="coerce").fillna(0).sum()
    B, R, CS, PO = num("Due YTD"), num("Renewed YTD"), num("Cars sold YTD"), num("Policies YTD")
    total = {"Code": f"Σ {group}", "Dealer": f"{len(codes)} codes", "Due YTD": B, "Renewed YTD": R, "Retention YTD": ratio(R, B),
             "Cars sold YTD": CS, "Policies YTD": PO, "Penetration YTD": ratio(PO, CS) if CS else None,
             "To target": max(0, math.ceil(t["TOT"] * B - R))}
    if f is not None:
        total.update({"First year YTD": _agg(f["ytd"])["FY %"], "Other year YTD": _agg(f["ytd"])["OY %"],
                      "Last month": _agg(f["lm"])["Ret %"], "MTD": _agg(f["mtd"])["Ret %"]})
    lt = pd.concat([lt.sort_values("Retention YTD", na_position="last"), pd.DataFrame([total])], ignore_index=True)
    rep.table(lt, "Each code, then the group total")
    have = lt.iloc[:-1].dropna(subset=["Retention YTD"])
    if len(have) > 1:
        lo, hi = have.iloc[0], have.iloc[-1]
        rep.bullets([f"Weakest code **{lo['Code']}** at {A.pct(lo['Retention YTD'])} vs strongest **{hi['Code']}** at "
                     f"{A.pct(hi['Retention YTD'])} - a {(hi['Retention YTD'] - lo['Retention YTD']) * 100:.0f} pt spread inside one group.",
                     f"Lifting the weakest code to the group average adds about "
                     f"{A.n(max(0, (ratio(R, B) - lo['Retention YTD']) * (lo['Due YTD'] or 0)))} renewals."], "Inside the group")
    # payouts
    pay = pack.get("pc_payouts")
    if len(pay):
        u = pay[(~pay["paid"]) & pay["dealer_code"].isin(codes)]
        if len(u):
            rep.h("Payout pending")
            rep.table(u.groupby(["dealer_code", "status"], as_index=False).agg(Lines=("base_value", "size"), Value=("base_value", "sum"))
                      .rename(columns={"dealer_code": "Code", "status": "Status"}))
    return rep


# --------------------------------------------------------------------------- #
# RM / ZM follow-up: critical dealers with last visit, commission (payout) status
# --------------------------------------------------------------------------- #
VISIT_STALE_DAYS = 30


def last_visits(pack) -> pd.Series:
    """dealer_code -> last visit date, from any visit report in Drive (empty when none is there)."""
    v = pack.get("re_visits")
    if v.empty or "date" not in v: return pd.Series(dtype="datetime64[ns]")
    v = v[v["dealer_code"].astype(str) != ""]
    d = pd.to_datetime(v["date"], errors="coerce")
    ok = d <= pd.Timestamp(dt.date.today())              # a visit dated in the future is a typing error - ignore it
    return d[ok].groupby(v.loc[ok, "dealer_code"].astype(str).str.upper()).max()


def critical_dealers(pack, oem, zm=None, rm=None) -> tuple[pd.DataFrame, bool]:
    """Red dealers (below benchmark, with a real base) in scope, worst gap first, with last visit date.
    Returns (table, visit_data_available)."""
    if oem == "OVERALL":
        parts = [critical_dealers(pack, o, zm, rm)[0].assign(OEM=OEM_LABEL[o]) for o in ORDER]
        parts = [p for p in parts if len(p)]
        t = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        return (t.sort_values("Gap (pts)") if len(t) else t), not last_visits(pack).empty
    _, sc = scorecard(pack, oem, zm, rm, dealers=True)
    if sc.empty: return sc, not last_visits(pack).empty
    if "Level" in sc: sc = sc[sc["Level"].isin(["Dealer", "Group"]) | sc["Level"].isna()]
    due_col = next((c for c in ("Due YTD", "Due so far (MTD)", "Due MTD (FY)") if c in sc), None)
    rate_col = next((c for c in ("Conversion MTD (due so far)", "Retention YTD") if c in sc and sc[c].notna().any()), None)
    x = sc[(sc["RAG"] == "Red") & (pd.to_numeric(sc[due_col], errors="coerce").fillna(0) >= A.MIN_BASE)] if due_col else sc[sc["RAG"] == "Red"]
    lv = last_visits(pack)
    today = pd.Timestamp(dt.date.today())
    rows = []
    for _, r in x.sort_values("Gap (pts)").iterrows():
        code = str(r["_id"]).upper()
        when = lv.get(code) if len(lv) else None
        days = int((today - when).days) if when is not None and when == when else None
        rows.append({"Dealer": r["Name"], "Retention": r.get(rate_col) if rate_col else None, "Target %": r.get("Target"),
                     "Gap (pts)": r.get("Gap (pts)"), "Renewals to target": r.get("NOP to target"),
                     "Last RM visit": when.strftime("%d %b %Y") if days is not None else ("not visited" if len(lv) else "no visit report in Drive"),
                     "Days since visit": days,
                     "Follow-up": ("Visit now - not seen in " + str(days) + " days" if days is not None and days > VISIT_STALE_DAYS else
                                   "Visit now - no visit on record" if len(lv) and days is None else
                                   "Visited recently - review the plan" if days is not None else "Add the visit report to Drive"),
                     "_id": code})
    return pd.DataFrame(rows), not lv.empty


def commission_status(pack, oem, zm=None, rm=None, min_invoices=2) -> pd.DataFrame:
    """Dealers in scope with 2 or more unpaid commission (payout) invoices, any reason."""
    from .views import unpaid_invoices
    u = unpaid_invoices(pack, None if oem == "OVERALL" else oem, zm)
    if u.empty: return u
    if rm: u = u[u["rm"] == rm]
    g = u.groupby(["dealer_code", "dealer_name"]).agg(**{"Unpaid invoices": ("value", "size"), "Pending value": ("value", "sum"),
                                                          "Oldest month": ("month", "min")})
    reason = u.groupby(["dealer_code", "dealer_name"])["reason"].agg(lambda s: s.value_counts().index[0])
    g["Main reason"] = reason
    g = g[g["Unpaid invoices"] >= min_invoices].reset_index().sort_values(["Unpaid invoices", "Pending value"], ascending=False)
    from .mailer import action_for
    stat = u.groupby("dealer_code")["status"].agg(lambda s: s.value_counts().index[0])
    g["What unblocks it"] = g["dealer_code"].map(stat).map(action_for)
    g["Oldest month"] = [month_label(m) if isinstance(m, str) else "" for m in g["Oldest month"]]
    return g.rename(columns={"dealer_code": "Code", "dealer_name": "Dealer"})


def follow_up_report(pack, oem, zm=None, rm=None) -> Report:
    """The RM / ZM action list: critical dealers with last visit, and commission pending."""
    scope = " · ".join(x for x in (PAGE_LABEL.get(oem, oem), (zm or "").title(), (rm or "").title()) if x)
    rep = Report(f"Follow-up list - {scope}", "Critical dealers with their last RM visit, and dealers with commission pending")
    crit, has_visits = critical_dealers(pack, oem, zm, rm)
    rep.h(f"Critical dealers - below benchmark ({len(crit)})")
    if not has_visits:
        rep.bullets(["No dealer visit report is in the Google Drive folder, so last-visit dates are blank. Add the visit report "
                     "(e.g. 'RE_DEALER VISIT REPORT ...') and the dates fill in automatically."])
    if len(crit):
        stale = int(crit["Follow-up"].str.startswith("Visit now").sum())
        if has_visits:
            rep.kpis([("Critical dealers", A.n(len(crit)), "Red, with at least the minimum base"),
                      ("Not visited in 30 days", A.n(stale), "visit first"),
                      ("Renewals to target", A.n(pd.to_numeric(crit["Renewals to target"], errors="coerce").sum()), "across these dealers")])
        rep.table(_fmt_export(crit.drop(columns=["_id"])))
    else:
        rep.p("No dealer is below benchmark in this scope.")
    com = commission_status(pack, oem, zm, rm)
    rep.h(f"Commission status - dealers with 2 or more unpaid invoices ({len(com)})")
    if len(com):
        rep.kpis([("Dealers", A.n(len(com)), "2+ unpaid invoices"), ("Unpaid invoices", A.n(com["Unpaid invoices"].sum()), ""),
                  ("Pending value", A.inr(com["Pending value"].sum()), "base value")])
        rep.table(com)
    else:
        rep.p("No dealer in this scope has 2 or more unpaid invoices.")
    return rep


# --------------------------------------------------------------------------- #
# Volvo: NCB vs non-NCB retention, Safenet programme share
# --------------------------------------------------------------------------- #
def _volvo_scope_codes(pack, zm=None, rm=None):
    r = pack.get("retention")
    v = r[r["oem"] == "VOLVO"]
    if zm: v = v[v["zm"] == zm]
    if rm: v = v[v["rm"] == rm]
    return set(v["dealer_code"].astype(str).str.upper()) if (zm or rm) else None


def volvo_ncb(pack, zm=None, rm=None) -> dict | None:
    """{'NCB': {...}, 'Non-NCB': {...}} with YTD (closed months) and running-month retention, first / other year."""
    n = pack.get("volvo_ncb_month")
    if n.empty: return None
    codes = _volvo_scope_codes(pack, zm, rm)
    if codes is not None: n = n[n["dealer_code"].astype(str).str.upper().isin(codes)]
    r = pack.get("retention"); v = r[r["oem"] == "VOLVO"]
    last, _, cur = A.months(v) if len(v) else (None, None, None)
    if not (last or cur): return None
    year = (last or cur)[:4]
    out = {}
    for flag in ("NCB", "Non-NCB"):
        x = n[n["ncb"] == flag]
        y = x[(x["month"] >= f"{year}-01") & (x["month"] <= (last or cur))]
        m = x[x["month"] == cur] if cur else x.iloc[0:0]
        seg = {sg: ratio(y[y["segment"] == sg]["ach"].sum(), y[y["segment"] == sg]["base"].sum()) for sg in ("FY", "OY")}
        out[flag] = {"ytd": ratio(y["ach"].sum(), y["base"].sum()), "base": y["base"].sum(), "ach": y["ach"].sum(),
                     "mtd": ratio(m["ach"].sum(), m["base"].sum()) if len(m) else None, "fy": seg["FY"], "oy": seg["OY"],
                     "label": f"YTD {month_label(y['month'].min())}-{month_label(last or cur)}" if len(y) else ""}
    return out


def volvo_ncb_table(pack, zm=None, rm=None) -> pd.DataFrame:
    n = pack.get("volvo_ncb_month")
    if n.empty: return n
    codes = _volvo_scope_codes(pack, zm, rm)
    if codes is not None: n = n[n["dealer_code"].astype(str).str.upper().isin(codes)]
    r = pack.get("retention"); v = r[r["oem"] == "VOLVO"]
    last, _, cur = A.months(v)
    n = n[n["month"] <= (cur or last)]
    g = n.pivot_table(index="month", columns="ncb", values=["base", "ach"], aggfunc="sum", fill_value=0)
    out = pd.DataFrame({"Month": [month_label(m) + (" (to date)" if m == cur else "") for m in g.index]})
    for flag in ("NCB", "Non-NCB"):
        if ("base", flag) in g:
            out[f"{flag} due"] = g[("base", flag)].values
            out[f"{flag} renewed"] = g[("ach", flag)].values
            out[f"{flag} %"] = [ratio(a, b) for a, b in zip(g[("ach", flag)], g[("base", flag)])]
    return out


def volvo_safenet(pack, zm=None, rm=None) -> dict | None:
    sf = pack.get("volvo_safenet")
    if sf.empty: return None
    codes = _volvo_scope_codes(pack, zm, rm)
    if codes is not None: sf = sf[sf["dealer_code"].astype(str).str.upper().isin(codes)]
    tot = sf["safenet"].sum() + sf["non_safenet"].sum()
    return {"share": ratio(sf["safenet"].sum(), tot) if tot else None, "safenet": sf["safenet"].sum(), "non_safenet": sf["non_safenet"].sum(),
            "cohort": sf["cohort_policies"].sum(), "cohort_label": (sf["cohort"].iloc[0] if len(sf) else "").replace("Polciies", "Policies"),
            "renewed_label": (sf["label"].iloc[0] if len(sf) else "").replace("No. of Safenet Policies", "").strip()}


# --------------------------------------------------------------------------- #
# Royal Enfield earnings (programme rates: dealer Rs 240, RM / ZM Rs 24 per retained policy)
# --------------------------------------------------------------------------- #
RS_DEALER, RS_MANAGER = 240, 24


def re_earnings(pack, zm=None, rm=None, rate=RS_MANAGER, dealer_code=None) -> dict | None:
    """Earned = retained policies x rate; at target = (month base x overall target) x rate; potential left = gap."""
    t = TARGETS["ROYAL ENFIELD"]["TOT"]
    if dealer_code is not None:
        dm = pack.get("re_dailymail")
        x = dm[dm["dealer_code"] == dealer_code] if len(dm) else dm
        if x.empty: return None
        base, ren = x[["fy_base", "oy_base"]].sum().sum(), x[["fy_ach", "oy_ach"]].sum().sum()
    else:
        from .views import re_mtd
        m = re_mtd(pack, zm, rm)
        base, ren = m.get("Base", 0), m.get("Renewed", 0)
        dm = pack.get("re_dailymail")
        if (zm or rm) and len(dm):                         # dealer-level file gives exact zone / RM totals
            x = dm
            if zm: x = x[x["zm"] == zm]
            if rm: x = x[x["rm"] == rm]
            base, ren = x[["fy_base", "oy_base"]].sum().sum(), x[["fy_ach", "oy_ach"]].sum().sum()
    if not base: return None
    earned, at_target = ren * rate, base * t * rate
    return {"rate": rate, "earned": earned, "at_target": at_target, "left": max(0.0, at_target - earned), "renewed": ren, "base": base}


# --------------------------------------------------------------------------- #
# Talking points (boom-call style): one card per unit one level down, red flags, wins, need vs pace
# --------------------------------------------------------------------------- #
def need_vs_pace(pack, oem, zm=None, rm=None) -> dict | None:
    """Running month: renewals needed per remaining day to reach target vs renewals per day so far."""
    from .views import summary
    if oem in (None, "OVERALL"): return None
    book = "Royal Enfield" if oem == "ROYAL ENFIELD" else "Private car"
    try:
        asof = dt.date.fromisoformat(str(pack.asof.get(book, ""))[:10])
    except ValueError:
        return None
    dim = (dt.date(asof.year + (asof.month == 12), asof.month % 12 + 1, 1) - dt.timedelta(days=1)).day
    left = max(1, dim - asof.day)
    t = TARGETS.get(oem, TARGETS["SKODA"])["TOT"]
    m = summary(pack, oem, zm, rm)["mtd"]
    if not m.get("Base"): return None
    gap = max(0.0, t * m["Base"] - m["Renewed"])
    return {"need": float(gap / left), "pace": float(m["Renewed"] / max(1, asof.day)), "left": int(left), "day": int(asof.day), "dim": int(dim), "gap": float(gap)}


def talking_points(pack, oem, zm=None, rm=None) -> dict:
    """{'headline', 'cards': [...], 'flags': [...], 'wins': [...], 'bars': ChartSpec} for the scope's next level down."""
    level, sc = scorecard(pack, oem, zm, rm)
    out = {"level": level, "cards": [], "flags": [], "wins": [], "bars": None, "headline": ""}
    if sc.empty: return out
    if "Level" in sc: sc = sc[sc["Level"].isin(["Dealer", "Group"]) | sc["Level"].isna()]
    rate_col = "Conversion MTD (due so far)" if "Conversion MTD (due so far)" in sc else "Retention YTD"
    lm_col = "Same date last month" if "Same date last month" in sc else "Last month"
    tgt_all = {"OEM": None}
    geo = dealer_geo(pack, oem, zm, rm) if level != "OEM" else pd.DataFrame()
    lv = last_visits(pack)
    # one card per unit, worst first
    rows = sc.dropna(subset=["Gap (pts)"]).sort_values("Gap (pts)")
    for _, r in rows.iterrows():
        name = str(r["Name"])
        rate, lm, tgt = r.get(rate_col), r.get(lm_col), r.get("Target")
        need = r.get("NOP to target")
        delta = (rate - lm) if (rate == rate and lm is not None and lm == lm and rate is not None) else None
        status = "r" if r.get("RAG") == "Red" else "a" if r.get("RAG") == "Amber" else "g"
        stats = [f"{'Conv' if rate_col.startswith('Conv') else 'Ret'} <b>{A.pct(rate)}</b>", f"target {A.pct(tgt)}",
                 f"gap <b>{A.n(need)}</b>" if need == need and need is not None else ""]
        if delta is not None: stats.append(f"<b class='{'bad-t' if delta < 0 else 'good-t'}'>{delta * 100:+.1f}</b> pts vs {('same date last month' if lm_col.startswith('Same') else 'last month')}")
        pts, ask = [], ""
        if r.get("Penetration YTD") == r.get("Penetration YTD") and r.get("Penetration YTD") is not None:
            pts.append(f"Penetration YTD {A.pct(min(r['Penetration YTD'], 9.99)) if r['Penetration YTD'] <= 1 else '100%+'}")
        if level == "OEM":
            o = next((k for k, v in OEM_LABEL.items() if v == name), None)
            if o:
                from .review import headline as _hl
                h_ = _hl(pack, o, zm)
                if h_:
                    need = h_["need"]
                    stats = [x for x in stats if not x.startswith("gap")] + [f"gap <b>{A.n(need)}</b>"]
                    if h_["gap"] is not None and h_["gap"] <= -.15:
                        out["flags"].append(f"{name}: {A.pct(h_['rate'])} - {abs(h_['gap']) * 100:.0f} pts below the {A.pct(h_['target'])} target, "
                                            f"{A.n(need)} renewals short.")
                    elif h_["gap"] is not None and h_["gap"] >= 0:
                        out["wins"].append(f"{name}: {A.pct(h_['rate'])}, at or above the {A.pct(h_['target'])} target.")
                lv_, sc2 = scorecard(pack, o, zm)
                if len(sc2) and "Gap (pts)" in sc2:
                    w = sc2.dropna(subset=["Gap (pts)"]).sort_values("Gap (pts)")
                    if len(w): pts.append(f"Weakest {lv_}: <b>{str(w.iloc[0]['Name']).title()}</b> ({w.iloc[0]['Gap (pts)']:+.0f} pts)")
                nv = need_vs_pace(pack, o, zm)
                if nv: pts.append(f"This month needs <b>{nv['need']:.0f}/day</b> for {nv['left']} days; running {nv['pace']:.0f}/day")
                ask = f"Close the {A.n(need)}-renewal gap: weekly review of the weakest {lv_ if lv_.isupper() else lv_.lower()}s."
        else:
            key = {"ZM": "ZM", "RM": "RM", "Dealer": "Code"}[level]
            ident = str(r.get("_id", name))
            mine = geo[geo[key].astype(str).str.upper() == ident.upper()] if len(geo) and key in geo else pd.DataFrame()
            if level == "Dealer":
                when = lv.get(ident.upper()) if len(lv) else None
                pts.append(f"Last RM visit {when:%d %b}" if when is not None and when == when else ("No RM visit on record" if len(lv) else ""))
                ask = f"Recover {A.n(need)} renewals" + (" - visit this week" if (when is None or (pd.Timestamp(dt.date.today()) - when).days > VISIT_STALE_DAYS) else "")
            elif len(mine):
                red = mine[(mine["RAG"] == "Red") & (mine["Due"] >= A.MIN_BASE)]
                top = mine.sort_values("To target", ascending=False).head(3)
                pts.append(f"{len(mine)} dealers · <b>{len(red)}</b> below benchmark")
                if len(top): pts.append("Largest gaps: " + "; ".join(f"{str(d['Dealer'])[:28]} ({A.n(d['To target'])})" for _, d in top.iterrows()))
                if len(lv):
                    stale = [c for c in red["Code"].astype(str).str.upper() if (lv.get(c) is None) or (pd.Timestamp(dt.date.today()) - lv.get(c)).days > VISIT_STALE_DAYS]
                    if stale: pts.append(f"<b>{len(stale)}</b> red dealers not visited in {VISIT_STALE_DAYS} days")
                share = top["To target"].sum() / max(1, mine["To target"].sum()) if len(top) else 0
                ask = (f"Recover {A.n(need)} renewals - the top 3 dealers are {share:.0%} of it: "
                       + ", ".join(str(d)[:22] for d in top["Dealer"]) + ".") if len(top) else ""
        out["cards"].append({"title": name.title() if level != "Dealer" else name, "status": status, "stats": [x for x in stats if x],
                             "points": [p for p in pts if p], "ask": ask})
    # flags and wins
    if len(geo):
        g = geo[geo["Due"] >= max(A.MIN_BASE, 30)]
        for _, d in g.sort_values("Retention").head(4).iterrows():
            if d["Retention"] == d["Retention"] and d["Retention"] < d["Target"] * .5:
                out["flags"].append(f"{d['Dealer']} ({str(d['RM']).title()}): {A.pct(d['Retention'])} on {A.n(d['Due'])} due - under half the target.")
        for _, d in g.sort_values("Retention", ascending=False).head(4).iterrows():
            if d["Retention"] == d["Retention"] and d["Retention"] >= d["Target"]:
                out["wins"].append(f"{d['Dealer']} ({str(d['RM']).title()}): {A.pct(d['Retention'])} on {A.n(d['Due'])} due - at or above target.")
    for c, (_, r) in zip(out["cards"], rows.iterrows()):
        lm = r.get(lm_col); rate = r.get(rate_col)
        if lm is not None and lm == lm and rate == rate:
            if rate - lm <= -.10: out["flags"].append(f"{c['title']}: down {abs(rate - lm) * 100:.0f} pts vs {lm_col.lower()}.")
            if rate - lm >= .05: out["wins"].append(f"{c['title']}: up {(rate - lm) * 100:.0f} pts vs {lm_col.lower()}.")
    out["flags"], out["wins"] = out["flags"][:6], out["wins"][:6]
    x = rows.sort_values("Gap (pts)")
    if len(x) and level != "Dealer":
        if x["Target"].nunique() > 1:      # mixed targets (e.g. OEMs): compare each with its own target
            out["bars"] = ChartSpec("hbar", [str(v).title() for v in x["Name"]],
                                    {"Vs own target": [float(v) / 100 if v == v else None for v in x["Gap (pts)"]]},
                                    f"{level}s - points above / below their own target", pct=True, target=0.0)
        else:
            out["bars"] = ChartSpec("hbar", [str(v).title() for v in x["Name"]], {"Result": [float(v) if v == v else None for v in x[rate_col]]},
                                    f"{level}s against target", pct=True, target=float(x["Target"].median()))
    below = int((rows["RAG"] == "Red").sum())
    worst = rows.iloc[0] if len(rows) else None
    tot_need = pd.to_numeric(rows.get("NOP to target"), errors="coerce").fillna(0).sum() if "NOP to target" in rows else 0
    out["headline"] = (f"{below} of {len(rows)} {level if level.isupper() else level.lower()}s are red" + (f"; weakest <b>{str(worst['Name']).title()}</b> "
                       f"({worst['Gap (pts)']:+.0f} pts)" if worst is not None else "") +
                       (f"; <b>{A.n(tot_need)}</b> renewals to target." if tot_need else "."))
    return out
