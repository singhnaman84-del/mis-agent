"""RM and ZM performance reviews (consulting-style: answer first, evidence, then actions) and the cross tie-up views.

RM review  : executive summary -> scorecard per tie-up vs zone / national -> per tie-up: month on month, where the gap
             sits (dealers ranked by renewals still needed, cumulative share), critical dealers with last visit,
             commission pending -> action plan.
ZM review  : the same, one level up - RM league table per tie-up, zone vs national, RE regions, action plan by RM.
Cross views: RM x tie-up and ZM x tie-up matrices (Overall), RM league per OEM (OEM pages).
Royal Enfield's headline is conversion on policies due so far (MTD); cars use retention YTD on closed months.
"""
from __future__ import annotations

import math

import pandas as pd

from . import analytics as A
from .charts import ChartSpec
from .model import OEM_LABEL, TARGETS
from .report import Report
from .util import month_label, ratio

ORDER = ["ROYAL ENFIELD", "AUDI", "SKODA", "VOLKSWAGEN", "VOLVO"]


def _p(x): return "–" if x is None or x != x else f"{x * 100:.1f}%"
def _pc(x): return "–" if x is None or x != x else ("100%+" if x > 1 else f"{x * 100:.1f}%")
def _n(x):
    try: return f"{float(x):,.0f}"
    except (TypeError, ValueError): return "–"
def _pts(x): return "–" if x is None or x != x else f"{x * 100:+.1f}"


def oems_for(pack, rm=None, zm=None) -> list[str]:
    out = set()
    r = pack.get("retention")
    x = r[r["book"] == "Private car"]
    if rm: x = x[x["rm"] == rm]
    if zm: x = x[x["zm"] == zm]
    out |= set(x["oem"])
    s = pack.get("re_dealer_snapshot")
    if len(s):
        y = s
        if rm: y = y[y["rm"] == rm]
        if zm: y = y[y["zm"] == zm]
        if len(y): out.add("ROYAL ENFIELD")
    return [o for o in ORDER if o in out]


def headline(pack, oem, zm=None, rm=None) -> dict | None:
    """The one number per tie-up + its context, the same definition everywhere."""
    from .mgmt import pen_summary, re_due_so_far, re_earnings, volvo_ncb
    from .views import re_mtd, summary
    t = TARGETS.get(oem, TARGETS["SKODA"])
    s = summary(pack, oem, zm, rm)
    y, m, l = s["ytd"], s["mtd"], s["lm"]
    pn = pen_summary(pack, oem, zm, rm)
    out = {"oem": oem, "target": t["TOT"], "pen": pn["ytd"] if pn else None, "addon": pn.get("addon_ytd") if pn else None,
           "fy": None, "oy": None, "lm": l["Ret %"] if l.get("Base") else None, "mtd": m["Ret %"] if m.get("Base") else None, "earn": None}
    if oem == "ROYAL ENFIELD":
        d = re_due_so_far(pack, zm, rm)
        mm = re_mtd(pack, zm, rm)
        if not d.get("due") and not mm.get("Base"): return None
        out.update({"rate": d.get("conv") if d.get("due") else mm.get("Ret %"), "measure": "Conversion MTD (due so far)",
                    "base": d.get("due") or mm.get("Base"), "ren": d.get("renewed") or mm.get("Renewed"),
                    "prev": d.get("prev"), "fy": mm.get("FY %"), "oy": mm.get("OY %") if mm.get("OY base") else None})
        dm = pack.get("re_dailymail")
        if len(dm) and (zm or rm):                       # other year at zone / RM level from the dealer-wise daily mail
            q = dm
            if zm: q = q[q["zm"] == zm]
            if rm: q = q[q["rm"] == rm]
            if q["oy_base"].sum(): out["oy"] = ratio(q["oy_ach"].sum(), q["oy_base"].sum())
        ea = re_earnings(pack, zm, rm)
        if ea: out["earn"] = ea
    else:
        if not y.get("Base") and not m.get("Base"): return None
        b = y if y.get("Base") else m
        out.update({"rate": b["Ret %"], "measure": "Retention " + (s["ytd_label"] if y.get("Base") else s["mtd_label"]),
                    "base": b["Base"], "ren": b["Renewed"], "prev": None, "fy": b.get("FY %"), "oy": b.get("OY %")})
        if oem == "VOLVO":
            nc = volvo_ncb(pack, zm, rm)
            if nc: out["ncb"], out["non_ncb"] = nc["NCB"]["ytd"], nc["Non-NCB"]["ytd"]
    out["gap"] = (out["rate"] - t["TOT"]) if out["rate"] == out["rate"] and out["rate"] is not None else None
    out["need"] = max(0, math.ceil(t["TOT"] * (out["base"] or 0) - (out["ren"] or 0)))
    out["rag"] = A.rag(out["rate"], t["TOT"])
    return out


def _scorecard_rows(pack, oems, zm=None, rm=None, bench=True) -> pd.DataFrame:
    rows = []
    for o in oems:
        h = headline(pack, o, zm, rm)
        if not h: continue
        zb = headline(pack, o, zm) if (bench and rm and zm) else None
        nb = headline(pack, o) if bench else None
        row = {"Tie-up": OEM_LABEL[o], "Measure": h["measure"], "Result": _p(h["rate"]), "Target": _p(h["target"]), "Gap (pts)": _pts(h["gap"])}
        if zb: row["Zone avg"] = _p(zb["rate"])
        if nb: row["National avg"] = _p(nb["rate"])
        row.update({"First year": _p(h["fy"]), "Other year": _p(h["oy"]), "Last month": _p(h["lm"]) if o != "ROYAL ENFIELD" else _p(h.get("prev")),
                    "Penetration": _pc(h["pen"]), "Add-on": _p(h["addon"]) if o == "ROYAL ENFIELD" else "–",
                    "Renewals to target": _n(h["need"]),
                    "Earnings (Rs 24)": A.inr(h["earn"]["earned"]) if h.get("earn") else "–", "RAG": h["rag"]})
        if o == "VOLVO" and h.get("ncb") is not None:
            row["NCB / non-NCB"] = f"{_p(h['ncb'])} / {_p(h['non_ncb'])}"
        rows.append(row)
    return pd.DataFrame(rows)


def _pareto(pack, oem, zm=None, rm=None, top=10) -> pd.DataFrame:
    from .mgmt import scorecard
    _, sc = scorecard(pack, oem, zm, rm, dealers=True)
    if sc.empty or "NOP to target" not in sc: return pd.DataFrame()
    if "Level" in sc: sc = sc[sc["Level"].isin(["Dealer", "Group"]) | sc["Level"].isna()]
    sc = sc.assign(_need=pd.to_numeric(sc["NOP to target"], errors="coerce").fillna(0)).sort_values("_need", ascending=False)
    tot = sc["_need"].sum()
    if not tot: return pd.DataFrame()
    rate_col = "Conversion MTD (due so far)" if "Conversion MTD (due so far)" in sc else "Retention YTD"
    x = sc.head(top)
    return pd.DataFrame({"Dealer": x["Name"], "Rate": [_p(v) for v in x[rate_col]], "Renewals to target": [_n(v) for v in x["_need"]],
                         "Share of zone/RM gap": [_p(v / tot) for v in x["_need"]], "Cumulative": [_p(v / tot) for v in x["_need"].cumsum()]})


def _actions(rows: list[dict]) -> pd.DataFrame:
    rows = [dict(r, **{"#": i + 1}) for i, r in enumerate(rows)]
    return pd.DataFrame(rows)[["#", "Action", "Owner", "By when"]] if rows else pd.DataFrame()


# --------------------------------------------------------------------------- #
# RM review
# --------------------------------------------------------------------------- #
def rm_review(pack, rm: str, oem: str = "OVERALL") -> Report:
    from .mgmt import commission_status, critical_dealers, month_table, trend
    oems = [o for o in oems_for(pack, rm=rm) if oem in (None, "OVERALL", o)]
    r = pack.get("retention")
    zm = r[r["rm"] == rm]["zm"].mode()
    snap = pack.get("re_dealer_snapshot")
    if zm.empty and len(snap): zm = snap[snap["rm"] == rm]["zm"].mode()
    zm = zm.iloc[0] if len(zm) else None
    scope = "all tie-ups" if oem in (None, "OVERALL") else OEM_LABEL.get(oem, oem)
    rep = Report(f"RM performance review - {rm.title()}", f"{scope} · zone {str(zm or '').title()} · tie-ups: "
                 f"{', '.join(OEM_LABEL[o] for o in oems) or 'none'} · " + " · ".join(f"{k} as of {v}" for k, v in pack.asof.items()))
    if not oems: return rep.p("No data for this RM in the current files.")
    heads = {o: headline(pack, o, zm, rm) for o in oems}
    heads = {o: h for o, h in heads.items() if h}
    zone_h = {o: headline(pack, o, zm) for o in heads}
    crit = {o: critical_dealers(pack, o, zm, rm)[0] for o in heads}
    com = {o: commission_status(pack, o, zm, rm) for o in heads}
    # executive summary
    msgs = []
    below = [o for o, h in heads.items() if h["gap"] is not None and h["gap"] < 0]
    if heads:
        msgs.append(f"**{len(below)} of {len(heads)} tie-ups below target.** " + "; ".join(
            f"{OEM_LABEL[o]} {_p(heads[o]['rate'])} vs {_p(heads[o]['target'])}" for o in heads))
    ahead = [OEM_LABEL[o] for o in heads if zone_h.get(o) and heads[o]["rate"] is not None and zone_h[o]["rate"] is not None and heads[o]["rate"] >= zone_h[o]["rate"]]
    behind = [OEM_LABEL[o] for o in heads if zone_h.get(o) and heads[o]["rate"] is not None and zone_h[o]["rate"] is not None and heads[o]["rate"] < zone_h[o]["rate"]]
    if ahead or behind:
        msgs.append("Versus the zone average: " + (f"ahead in **{', '.join(ahead)}**" if ahead else "") + ("; " if ahead and behind else "")
                    + (f"behind in **{', '.join(behind)}**" if behind else "") + ".")
    need_tot = sum(h["need"] for h in heads.values())
    if need_tot:
        worst = max(heads, key=lambda o: heads[o]["need"])
        par = _pareto(pack, worst, zm, rm, top=5)
        share = par["Cumulative"].iloc[-1] if len(par) else "–"
        msgs.append(f"**{_n(need_tot)} renewals** still needed to reach target; the largest gap is **{OEM_LABEL[worst]}** "
                    f"({_n(heads[worst]['need'])}), where the top 5 dealers hold **{share}** of it.")
    stale = sum(int(c["Follow-up"].str.startswith("Visit now").sum()) for c in crit.values() if len(c))
    ncrit = sum(len(c) for c in crit.values())
    if ncrit: msgs.append(f"**{ncrit} critical dealers** (below benchmark); **{stale}** not visited in the last 30 days.")
    ncom = sum(len(c) for c in com.values()); vcom = sum(c["Pending value"].sum() for c in com.values() if len(c))
    if ncom: msgs.append(f"**{ncom} dealers** have 2 or more unpaid commission invoices ({A.inr(vcom)}) - a lever for the dealer conversation.")
    rep.bullets(msgs, "Executive summary")
    rep.h("Scorecard by tie-up - versus zone and national")
    rep.table(_scorecard_rows(pack, list(heads), zm, rm))
    # per tie-up evidence
    for o, h in heads.items():
        rep.h(f"{OEM_LABEL[o]} - {h['measure']} {_p(h['rate'])} ({_pts(h['gap'])} pts vs target)")
        tr = trend(pack, o, zm, rm)
        if tr is not None: rep.chart(tr)
        mt = month_table(pack, o, zm, rm)
        if len(mt):
            mt = mt.copy()
            for c in mt.columns:
                if str(c).endswith("%"): mt[c] = [_pc(v) if "enetration" in c else _p(v) for v in mt[c]]
            rep.table(mt, "Month on month")
        par = _pareto(pack, o, zm, rm)
        if len(par): rep.table(par, "Where the gap sits - dealers by renewals still needed")
        c = crit.get(o)
        if c is not None and len(c):
            cc = c.drop(columns=["_id"], errors="ignore").copy()
            for k in ("Retention", "Target %"):
                if k in cc: cc[k] = [_p(v) for v in cc[k]]
            if "Gap (pts)" in cc: cc["Gap (pts)"] = [f"{v:+.1f}" if v == v else "" for v in pd.to_numeric(cc["Gap (pts)"], errors="coerce")]
            rep.table(cc, f"Critical dealers ({len(cc)}) with last RM visit")
        cm = com.get(o)
        if cm is not None and len(cm):
            rep.table(cm.assign(**{"Pending value": cm["Pending value"].map(A.inr)}), f"Commission - dealers with 2+ unpaid invoices ({len(cm)})")
    # action plan
    acts = []
    for o, h in heads.items():
        if h["need"]:
            par = _pareto(pack, o, zm, rm, top=5)
            acts.append({"Action": f"{OEM_LABEL[o]}: recover {_n(h['need'])} renewals - start with the top 5 gap dealers"
                                   + (f" ({par['Cumulative'].iloc[-1]} of the gap)" if len(par) else ""), "Owner": rm.title(), "By when": "This month"})
        c = crit.get(o)
        if c is not None and len(c):
            s_ = int(c["Follow-up"].str.startswith("Visit now").sum())
            if s_: acts.append({"Action": f"{OEM_LABEL[o]}: visit {s_} critical dealers not seen in 30 days", "Owner": rm.title(), "By when": "2 weeks"})
        cm = com.get(o)
        if cm is not None and len(cm):
            acts.append({"Action": f"{OEM_LABEL[o]}: clear {int(cm['Unpaid invoices'].sum())} unpaid invoices at {len(cm)} dealers ({A.inr(cm['Pending value'].sum())})",
                         "Owner": f"{rm.title()} + dealer accounts", "By when": "This month"})
        if o == "ROYAL ENFIELD" and h.get("addon") is not None and h["addon"] < .85:
            acts.append({"Action": f"Royal Enfield: lift add-on penetration from {_p(h['addon'])} to 85%", "Owner": rm.title(), "By when": "Quarter"})
    rep.h("Action plan")
    rep.table(_actions(acts))
    return rep


# --------------------------------------------------------------------------- #
# ZM review
# --------------------------------------------------------------------------- #
def zm_review(pack, zm: str, oem: str = "OVERALL") -> Report:
    from .mgmt import commission_status, critical_dealers, geo_report, month_table, scorecard, trend
    oems = [o for o in oems_for(pack, zm=zm) if oem in (None, "OVERALL", o)]
    scope = "all tie-ups" if oem in (None, "OVERALL") else OEM_LABEL.get(oem, oem)
    rep = Report(f"ZM performance review - {zm.title()}", f"{scope} · tie-ups: {', '.join(OEM_LABEL[o] for o in oems) or 'none'} · "
                 + " · ".join(f"{k} as of {v}" for k, v in pack.asof.items()))
    if not oems: return rep.p("No data for this zone in the current files.")
    heads = {o: h for o in oems if (h := headline(pack, o, zm))}
    leagues = {}
    for o in heads:
        _, sc = scorecard(pack, o, zm)
        leagues[o] = sc
    msgs = []
    below = [o for o, h in heads.items() if h["gap"] is not None and h["gap"] < 0]
    msgs.append(f"**{len(below)} of {len(heads)} tie-ups below target.** " + "; ".join(
        f"{OEM_LABEL[o]} {_p(h['rate'])} vs {_p(h['target'])}" for o, h in heads.items()))
    nat = {o: headline(pack, o) for o in heads}
    vs = [f"{OEM_LABEL[o]} {_pts((heads[o]['rate'] or 0) - (nat[o]['rate'] or 0))} pts" for o in heads if nat.get(o) and nat[o]["rate"] is not None]
    if vs: msgs.append("Versus the national average: " + ", ".join(vs) + ".")
    for o, sc in leagues.items():
        if len(sc) >= 2 and "Gap (pts)" in sc:
            x = sc.dropna(subset=["Gap (pts)"]).sort_values("Gap (pts)")
            if len(x):
                msgs.append(f"{OEM_LABEL[o]}: **{int((x['RAG'] == 'Red').sum())} of {len(x)} RMs Red**; weakest **{str(x.iloc[0]['Name']).title()}** "
                            f"({float(x.iloc[0]['Gap (pts)']):+.0f} pts), strongest **{str(x.iloc[-1]['Name']).title()}** ({float(x.iloc[-1]['Gap (pts)']):+.0f} pts).")
    need_tot = sum(h["need"] for h in heads.values())
    if need_tot: msgs.append(f"**{_n(need_tot)} renewals** still needed across the zone to reach target.")
    rep.bullets(msgs[:6], "Executive summary")
    rep.h("Scorecard by tie-up - versus national")
    rep.table(_scorecard_rows(pack, list(heads), zm))
    for o, h in heads.items():
        rep.h(f"{OEM_LABEL[o]} - {h['measure']} {_p(h['rate'])} ({_pts(h['gap'])} pts vs target)")
        sc = leagues.get(o)
        if sc is not None and len(sc):
            keep = [c for c in ("Name", "Conversion MTD (due so far)", "Same date last month", "Retention YTD", "Last month", "MTD", "First year YTD",
                                "Penetration YTD", "Gap (pts)", "NOP to target", "RAG") if c in sc]
            lt = sc[keep].sort_values("Gap (pts)").copy()
            for c in keep:
                if c not in ("Name", "Gap (pts)", "NOP to target", "RAG"): lt[c] = [_pc(v) if "enetration" in c else _p(v) for v in lt[c]]
            lt["Gap (pts)"] = [f"{v:+.1f}" if v == v else "" for v in pd.to_numeric(lt["Gap (pts)"], errors="coerce")]
            rep.table(lt.rename(columns={"Name": "RM", "NOP to target": "Renewals to target"}), "RM league table")
        tr = trend(pack, o, zm)
        if tr is not None: rep.chart(tr)
        mt = month_table(pack, o, zm)
        if len(mt):
            mt = mt.copy()
            for c in mt.columns:
                if str(c).endswith("%"): mt[c] = [_pc(v) if "enetration" in c else _p(v) for v in mt[c]]
            rep.table(mt, "Month on month")
        if o == "ROYAL ENFIELD":
            g = geo_report(pack, o, "Region", zm)
            tb = next((b for b in g.blocks if b.kind == "table"), None)
            if tb is not None: rep.table(tb.df, "Regions in the zone")
        par = _pareto(pack, o, zm)
        if len(par): rep.table(par, "Where the gap sits - dealers by renewals still needed")
    # follow-up per RM
    rows = []
    for o in heads:
        c, _ = critical_dealers(pack, o, zm)
        cm = commission_status(pack, o, zm)
        sc = leagues.get(o, pd.DataFrame())
        for rm_ in (sc["_id"] if len(sc) and "_id" in sc else []):
            c_rm, _ = critical_dealers(pack, o, zm, rm_)
            cm_rm = commission_status(pack, o, zm, rm_)
            rows.append({"Tie-up": OEM_LABEL[o], "RM": str(rm_).title(), "Critical dealers": len(c_rm),
                         "Not visited 30 days": int(c_rm["Follow-up"].str.startswith("Visit now").sum()) if len(c_rm) else 0,
                         "Dealers 2+ unpaid": len(cm_rm), "Pending value": A.inr(cm_rm["Pending value"].sum()) if len(cm_rm) else "–"})
    if rows:
        rep.h("Follow-up by RM")
        rep.table(pd.DataFrame(rows).sort_values(["Tie-up", "Critical dealers"], ascending=[True, False]))
    acts = []
    for o, sc in leagues.items():
        if len(sc) and "Gap (pts)" in sc:
            x = sc.dropna(subset=["Gap (pts)"]).sort_values("Gap (pts)").head(3)
            for _, rr in x.iterrows():
                if rr["Gap (pts)"] < 0:
                    acts.append({"Action": f"{OEM_LABEL[o]}: weekly review with {str(rr['Name']).title()} - {_n(rr.get('NOP to target'))} renewals to target",
                                 "Owner": zm.title(), "By when": "Weekly"})
    rep.h("Action plan")
    rep.table(_actions(acts))
    return rep


# --------------------------------------------------------------------------- #
# cross views
# --------------------------------------------------------------------------- #
def cross_matrix(pack, level: str = "rm", zm=None) -> pd.DataFrame:
    """Rows = RMs (or ZMs); one column per tie-up with its headline result; plus tie-ups below target."""
    r = pack.get("retention"); s = pack.get("re_dealer_snapshot")
    if level == "rm":
        people = set(r[r["book"] == "Private car"].pipe(lambda x: x[x["zm"] == zm] if zm else x)["rm"])
        if len(s): people |= set((s[s["zm"] == zm] if zm else s)["rm"])
    else:
        people = set(r[r["book"] == "Private car"]["zm"]) | (set(s["zm"]) if len(s) else set())
    rows = []
    for who in sorted(people - {""}):
        row = {"RM" if level == "rm" else "ZM": who.title()}
        below = n = 0
        for o in ORDER:
            h = headline(pack, o, zm if level == "rm" else who, who if level == "rm" else None)
            if not h: continue
            n += 1; below += int(h["gap"] is not None and h["gap"] < 0)
            row[OEM_LABEL[o]] = h["rate"]
        if n:
            row["Tie-ups"] = n; row["Below target"] = below
            rows.append(row)
    t = pd.DataFrame(rows)
    if t.empty: return t
    first = "RM" if level == "rm" else "ZM"
    cols = [first] + [OEM_LABEL[o] for o in ORDER if OEM_LABEL[o] in t] + ["Tie-ups", "Below target"]
    return t[cols]


def cross_report(pack, level: str = "rm", zm=None) -> Report:
    lab = "RM" if level == "rm" else "ZM"
    rep = Report(f"{lab} performance across tie-ups" + (f" - {zm.title()}" if zm else ""),
                 "Headline per tie-up: Royal Enfield = conversion on policies due so far (MTD); cars = retention YTD (closed months)")
    t = cross_matrix(pack, level, zm)
    if t.empty: return rep.p("No data.")
    tg = {OEM_LABEL[o]: TARGETS.get(o, TARGETS["SKODA"])["TOT"] for o in ORDER}
    rep.bullets([f"Targets: " + ", ".join(f"{k} {_p(v)}" for k, v in tg.items() if k in t.columns) + ".",
                 f"{int((t['Below target'] == t['Tie-ups']).sum())} of {len(t)} {lab}s are below target in every tie-up they handle."])
    show = t.copy()
    for c in tg:
        if c in show: show[c] = [_p(v) for v in show[c]]
    rep.table(show.sort_values(["Below target", "Tie-ups"], ascending=[False, False]))
    return rep


def rm_league(pack, oem: str) -> pd.DataFrame:
    """Every RM of one OEM across all zones (OEM page)."""
    from .mgmt import scorecard
    parts = []
    for zm in sorted(set(pack.get("retention").query("oem == @oem")["zm"]) | (set(pack.get("re_dealer_snapshot")["zm"]) if oem == "ROYAL ENFIELD" else set())):
        if not zm: continue
        _, sc = scorecard(pack, oem, zm)
        if len(sc): parts.append(sc.assign(ZM=zm.title()))
    if not parts: return pd.DataFrame()
    t = pd.concat(parts, ignore_index=True)
    return t.sort_values("Gap (pts)")
