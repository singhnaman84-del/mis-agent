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

import math

import pandas as pd

from . import analytics as A
from . import groups as G
from .charts import ChartSpec
from .model import OEM_LABEL, TARGETS, re_zm
from .report import Report
from .util import month_label, ratio
from .views import CAR_OEMS, PAGE_LABEL, period_frames, re_group_table, summary, ytd_mtd_table

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
                "sold": p["veh_ytd"].sum(), "policies": p["prog_ytd"].sum()}
    if oem == "VOLVO": return None
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
            cards.append({"title": OEM_LABEL[o], "per": s["ytd_label"] if y["Base"] else s["mtd_label"], "value": main,
                          "target": s["target"]["TOT"], "status": _status(main, s["target"]["TOT"]),
                          "lines": [(s["lm_label"], s["lm"]["Ret %"] if s["lm"]["Base"] else None),
                                    ("MTD", m["Ret %"] if m["Base"] else None)],
                          "pen": (pn["ytd_label"].split(" (")[0], pn["ytd"]) if pn else None})
        return cards
    s = summary(pack, oem, zm, rm); t = s["target"]; pn = pen_summary(pack, oem, zm, rm)
    y, m, l = s["ytd"], s["mtd"], s["lm"]
    base = y if y["Base"] else m
    for k, lab, tg in (("Ret %", "Retention", t["TOT"]), ("FY %", "First year", t["FY"]), ("OY %", "Other year", t["OY"])):
        bk = {"Ret %": "Base", "FY %": "FY base", "OY %": "OY base"}[k]
        if not base.get(bk): continue
        cards.append({"title": lab, "per": s["ytd_label"] if y["Base"] else s["mtd_label"], "value": base[k], "target": tg,
                      "status": _status(base[k], tg),
                      "lines": [(s["lm_label"], l[k] if l.get(bk) else None), ("MTD", m[k] if m.get(bk) else None)]})
    if pn:
        cards.append({"title": "Penetration", "per": pn["ytd_label"], "value": pn["ytd"], "target": None, "status": "",
                      "lines": [(pn["latest_label"].replace("Pen. ", ""), pn["latest"]), (pn["prev_label"], pn["prev"])] if pn["prev_label"]
                      else [(pn["latest_label"].replace("Pen. ", ""), pn["latest"])],
                      "sub": f"{pn['policies']:,.0f} policies on {pn['sold']:,.0f} vehicles"})
    need = max(0, math.ceil(t["TOT"] * base["Base"] - base["Renewed"]))
    cards.append({"title": "Renewals to target", "per": s["ytd_label"] if y["Base"] else s["mtd_label"], "value": None,
                  "count": need, "status": "", "lines": [], "sub": f"{base['Renewed']:,.0f} renewed of {base['Base']:,.0f} due"})
    return cards


def trend(pack, oem, zm=None, rm=None) -> ChartSpec | None:
    """Retention % and penetration % by month on one chart (both are percentages)."""
    if oem == "OVERALL":
        r = pack.get("retention")
        pc = r[r["oem"].isin(CAR_OEMS)]
        if zm: pc = pc[pc["zm"] == zm]
        if pc.empty: return None
        t = A.rollup(pc, ["month", "oem"]).pivot(index="month", columns="oem", values="Ret %").sort_index()
        return ChartSpec("line", [month_label(m) for m in t.index], {OEM_LABEL.get(c, c): list(t[c]) for c in t.columns},
                         "Retention % by month - car brands (current month = MTD)", pct=True)
    d = A.oem_frame(pack, oem, zm, rm)
    if d.empty: return None
    m = A.rollup(d, ["month"]).set_index("month").sort_index()
    _, _, cur = A.months(d)
    series = {"Overall": list(m["Ret %"]), "First year": list(m["FY %"]), "Other year": list(m["OY %"])}
    if oem != "ROYAL ENFIELD":
        p = pc_pen_frame(pack, oem, zm, rm)
        if not p.empty:
            pm = p.groupby("month")[["new_nop", "retail"]].sum()
            series["Penetration"] = [ratio(pm.at[x, "new_nop"], pm.at[x, "retail"]) if x in pm.index else None for x in m.index]
    title = f"{PAGE_LABEL.get(oem, oem)} - retention{' and penetration' if 'Penetration' in series else ''} by month"
    if oem == "ROYAL ENFIELD": title += " - North zone dealers (the monthly file covers Naman Singh's zone)"
    return ChartSpec("line", [month_label(x) + (" MTD" if x == cur else "") for x in m.index], series, title, pct=True,
                     target=TARGETS.get(oem, TARGETS["SKODA"])["TOT"])


def month_table(pack, oem, zm=None, rm=None) -> pd.DataFrame:
    """Month on month: first year, other year and overall retention (+ penetration for Audi / Skoda / VW)."""
    if oem == "OVERALL": return pd.DataFrame()
    d = A.oem_frame(pack, oem, zm, rm)
    if d.empty: return pd.DataFrame()
    _, _, cur = A.months(d)
    m = A.rollup(d, ["month"]).sort_values("month")
    out = pd.DataFrame({"Month": [month_label(x) + (" (MTD)" if x == cur else "") for x in m["month"]],
                        "FY due": m["FY base"], "FY renewed": m["FY ach"], "First year %": m["FY %"],
                        "OY due": m["OY base"], "OY renewed": m["OY ach"], "Other year %": m["OY %"],
                        "Total due": m["Base"], "Total renewed": m["Renewed"], "Overall %": m["Ret %"]})
    out["Change (pts)"] = out["Overall %"].diff() * 100
    if oem not in ("ROYAL ENFIELD", "VOLVO"):
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
        t = t.set_index(idc).join(pn[["Pen YTD %", "Pen latest %"]], how="left").reset_index()
        out = pd.DataFrame({"Name": t["Dealer"] + " · " + t["Code"] if key == "dealer_code" else t[idc],
                            "Retention YTD": t.get("FYTD % (North)"), "Last month": None,
                            "MTD": t["MTD FY %"], "First year YTD": t.get("FYTD FY % (North)"),
                            "Target": TARGETS["ROYAL ENFIELD"]["FY"], "Gap (pts)": (t["MTD FY %"] - TARGETS["ROYAL ENFIELD"]["FY"]) * 100,
                            "Penetration YTD": t["Pen YTD %"], "Penetration latest": t["Pen latest %"],
                            "Due YTD": t["MTD FY base"], "NOP to target": t["NOP to FY target"], "RAG": t["RAG"], "_id": t[idc]})
        out = out.rename(columns={"MTD": "MTD first year", "Due YTD": "Due MTD (FY)"})
        return level, out.sort_values("Gap (pts)")
    by = [key] + (["dealer_name"] if key == "dealer_code" else [])
    t = ytd_mtd_table(pack, oem, by, zm, rm)
    if t.empty: return level, t
    lm_col = next(c for c in t.columns if c.endswith(" %") and c not in ("YTD %", "FY YTD %", "OY YTD %", "Target %") and not c.startswith("MTD"))
    mtd_col = next(c for c in t.columns if c.startswith("MTD"))
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
        if d["MTD"].notna().any() and d["Last month"].notna().any():
            dd = (d["MTD"] - d["Last month"]).dropna()
            if len(dd): out.append(f"September MTD is behind August for {int((dd < 0).sum())} of {len(dd)} OEMs - the running month is not closed yet.")
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
            "attention": att, "best": best, "messages": messages(pack, oem, zm, rm, level, sc),
            "note": ("Royal Enfield: retention MTD (first year) is national; YTD is the North-zone history. "
                     "Penetration = program policies / bikes sold." if oem == "ROYAL ENFIELD" else
                     "Volvo has no penetration data in the MIS." if oem == "VOLVO" else
                     "Penetration = new policies / cars sold (SAVWIPL MIS, Audi / Skoda / VW)." if oem != "OVERALL" else
                     "Retention YTD: cars Jan to date; Royal Enfield April to date (North). Penetration and retention are separate books.")}


def to_report(v: dict) -> Report:
    rep = Report(v["title"], v["asof"])
    tiles = []
    for c in v["kpis"]:
        val = f"{c['count']:,}" if c.get("count") is not None else A.pct(c["value"])
        sub = " · ".join(f"{k} {A.pct(x)}" for k, x in c.get("lines", []) if x is not None)
        if c.get("pen"): sub += f" · {c['pen'][0]} {A.pct(c['pen'][1])}"
        tiles.append((f"{c['title']} - {c['per']}", val, sub or c.get("sub", "")))
    rep.kpis(tiles)
    if v["messages"]: rep.bullets(v["messages"], "Key messages")
    if v["trend"] is not None: rep.chart(v["trend"])
    if len(v.get("months", [])): rep.table(v["months"], "Month on month - first year, other year, overall")
    sc = v["score"].drop(columns=[c for c in v["score"].columns if c.startswith("_")], errors="ignore")
    if len(sc): rep.table(sc, f"{v['level']} scorecard - retention and penetration")
    for k, lab in (("attention", "Needs attention"), ("best", "Best performers")):
        if len(v[k]): rep.table(v[k][[c for c in ("Name", "Retention YTD", "Gap (pts)", "Penetration YTD", "NOP to target") if c in v[k]]], lab)
    rep.bullets([v["note"]])
    return rep


# --------------------------------------------------------------------------- #
# penetration report and group report
# --------------------------------------------------------------------------- #
def penetration_page(pack, oem, zm=None, rm=None) -> Report:
    scope = " · ".join(x for x in (PAGE_LABEL.get(oem, oem), (zm or "").title(), (rm or "").title()) if x)
    rep = Report(f"Penetration - {scope}", "Policies sold with the vehicle / vehicles sold. Clubbed groups: codes first, then the group total.")
    oems = [o for o in ORDER if o != "VOLVO"] if oem == "OVERALL" else [oem]
    if oem == "VOLVO":
        return rep.p("The MIS drop has no Volvo penetration data (the SAVWIPL penetration MIS covers Audi, Skoda and VW).")
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
            g = g.sort_values("Pen YTD %")
            rep.chart(ChartSpec("hbar", list(g[key]), {"Penetration YTD": list(g["Pen YTD %"])}, f"Penetration YTD by {key.upper()}", pct=True))
            rep.table(g.rename(columns={"zm": "ZM", "rm": "RM"}), f"By {key.upper()}")
            low = p[p["veh_mtd"] >= 20].assign(**{"Pen MTD %": lambda x: x["prog_mtd"] / x["veh_mtd"],
                                                  "Pen YTD %": lambda x: x["prog_ytd"] / x["veh_ytd"].where(x["veh_ytd"] > 0)})
            rep.table(low.sort_values("Pen MTD %").head(15)[["dealer_code", "dealer_name", "rm", "veh_mtd", "prog_mtd", "Pen MTD %", "Pen YTD %"]]
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
                            next((k for k in c.columns if k.startswith("MTD")), None), "Vehicles sold", "Policies", "Pen YTD %",
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
