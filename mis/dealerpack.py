"""Dealer review - one builder for the dealer meeting deck (dealer-facing) and the dealer dashboard (internal).

Storyline (answer first): headline vs target -> how you compare with the REGION and ZONE (average and median only,
never named peers) -> month on month -> penetration (+ add-on for Royal Enfield) -> earnings -> commission -> actions.
Internal view adds the RM / ZM, the last RM visit and the follow-up flag. Dealer-facing: no RM visits, no defaulter
labels, no customer data. Clubbed groups get one combined review (each outlet, then the group).
"""
from __future__ import annotations

import datetime as dt
import math

import pandas as pd

from . import analytics as A
from . import groups as G
from .charts import ChartSpec
from .model import OEM_LABEL, TARGETS
from .report import Report
from .util import month_label, ratio


def _p(x):
    return "–" if x is None or x != x else f"{x * 100:.1f}%"


def _pc(x):
    """Percent capped for penetration-type ratios that can exceed 100% (policies for earlier sales)."""
    return "–" if x is None or x != x else ("100%+" if x > 1 else f"{x * 100:.1f}%")


def _n(x):
    try: return f"{float(x):,.0f}"
    except (TypeError, ValueError): return "–"


def _pts(x):
    return "–" if x is None or x != x else f"{x * 100:+.1f} pts"


def _closed(d: pd.DataFrame):
    if d.empty: return d, None
    last, _, cur = A.months(d)
    top = last or cur
    if d["book"].iloc[0] == "Royal Enfield":
        return d[~d["is_mtd"].astype(bool)], cur
    return d[(~d["is_mtd"].astype(bool)) & (d["month"].str[:4] == (top or "")[:4])], cur


def _agg(d: pd.DataFrame) -> dict:
    out = {}
    for seg in ("FY", "OY"):
        x = d[d["segment"] == seg]
        out[f"{seg}b"], out[f"{seg}a"] = float(x["base"].sum()), float(x["ach"].sum())
    out["b"], out["a"] = out["FYb"] + out["OYb"], out["FYa"] + out["OYa"]
    out["r"], out["fy"], out["oy"] = ratio(out["a"], out["b"]), ratio(out["FYa"], out["FYb"]), ratio(out["OYa"], out["OYb"])
    return out


def _bench(df: pd.DataFrame, num: str, den: str):
    """(pooled average, median of dealer rates) over dealers with at least the minimum base."""
    if df is None or df.empty or num not in df or den not in df: return None, None
    x = df[pd.to_numeric(df[den], errors="coerce").fillna(0) >= A.MIN_BASE]
    if x.empty: return None, None
    return ratio(x[num].sum(), x[den].sum()), (x[num] / x[den]).median()


def _bench_table(rows: list[tuple]) -> pd.DataFrame:
    """rows: (measure, you, avg, median per level ..., labels tuple, cap)."""
    out = []
    for row in rows:
        m, you, *vals, labels, cap = row
        f = _pc if cap else _p
        d = {"Measure": m, "You": f(you)}
        for i, lab in enumerate(labels):
            d[f"{lab} average"], d[f"{lab} median"] = f(vals[2 * i]), f(vals[2 * i + 1])
        out.append(d)
    return pd.DataFrame(out)


def _codes_for(pack, code: str):
    r = pack.get("retention")
    hit = r[(r["dealer_code"].astype(str).str.upper() == code.upper()) & (r["book"] == "Private car")]
    if hit.empty: return None
    oem, name = hit["oem"].iloc[0], hit["dealer_name"].iloc[0]
    grp = G.group_of(name, oem)
    if not grp: return oem, [hit["dealer_code"].iloc[0]], name, None
    from .mgmt import group_codes
    codes = [c for c in group_codes(pack, oem, grp) if (r["dealer_code"] == c).any()] or [hit["dealer_code"].iloc[0]]
    return oem, codes, f"{grp} group", grp


def _payout_section(pack, rep: Report, codes: list[str]):
    from .mailer import action_for, unpaid_for
    rows = []
    for c in codes:
        u = unpaid_for(pack, str(c).upper())
        for x in u.itertuples():
            rows.append({"Code": c, "Invoice": x.inv, "Month": month_label(x.month) if isinstance(x.month, str) else "",
                         "Amount": float(x.value or 0), "Status": x.status, "Action needed": action_for(x.status)})
    rep.h("Commission (payout) status")
    if not rows:
        rep.p("No commission invoices are pending - all paid.")
        return 0, 0.0
    t = pd.DataFrame(rows).sort_values(["Month", "Invoice"])
    sched = t["Action needed"].str.startswith("No action")
    rep.kpis([("Unpaid invoices", _n(len(t)), "commission pending"), ("Amount pending", A.inr(t["Amount"].sum()), "base value"),
              ("Waiting on the dealer", _n(int((~sched).sum())), "invoices needing a dealer action")])
    show = t.assign(Amount=t["Amount"].map(A.inr))
    if len(codes) <= 1: show = show.drop(columns=["Code"])
    rep.table(show.head(40))
    if len(t) > 40: rep.p(f"... and {len(t) - 40} more invoices - full list from your RM.")
    return len(t), float(t["Amount"].sum())


def _internal_block(pack, rep: Report, codes, rm, zm):
    from .mgmt import VISIT_STALE_DAYS, last_visits
    lv = last_visits(pack)
    seen = [lv.get(str(c).upper()) for c in codes] if len(lv) else []
    seen = [v for v in seen if v is not None and v == v]
    when = max(seen) if seen else None
    days = (pd.Timestamp(dt.date.today()) - when).days if when is not None else None
    rep.h("Relationship (internal)")
    rep.kpis([("RM", str(rm).title() or "–", f"ZM {str(zm).title()}" if zm else ""),
              ("Last RM visit", when.strftime("%d %b %Y") if when is not None else ("not visited" if len(lv) else "no visit report"),
               f"{days} days ago" if days is not None else ""),
              ("Follow-up", "Visit now" if days is None or days > VISIT_STALE_DAYS else "On track",
               f"visits older than {VISIT_STALE_DAYS} days are flagged")])


def _actions(rep: Report, acts: list[dict]):
    acts = [dict(a, **{"#": i + 1}) for i, a in enumerate(acts)]
    while len(acts) < 5: acts.append({"#": len(acts) + 1, "Action": "", "Owner": "", "By when": ""})
    rep.h("Agreed actions")
    rep.table(pd.DataFrame(acts)[["#", "Action", "Owner", "By when"]])


def _mom_rows(mm: pd.DataFrame, cur) -> list[dict]:
    return [{"Month": month_label(v["month"]) + (" to date*" if v["month"] == cur else ""), "FY due": _n(v["FY base"]),
             "FY renewed": _n(v["FY ach"]), "First year": _p(v["FY %"]), "OY due": _n(v["OY base"]), "OY renewed": _n(v["OY ach"]),
             "Other year": _p(v["OY %"]), "Total due": _n(v["Base"]), "Renewed": _n(v["Renewed"]), "Overall": _p(v["Ret %"])}
            for _, v in mm.iterrows()]


# --------------------------------------------------------------------------- #
# private car (Audi / Skoda / VW / Volvo)
# --------------------------------------------------------------------------- #
def _car_review(pack, code, internal=False) -> Report:
    who = _codes_for(pack, code)
    if not who:
        return Report("Dealer review").p(f"No renewal data for dealer {code} in the current MIS.")
    oem, codes, name, grp = who
    t = TARGETS.get(oem, TARGETS["SKODA"])
    r = pack.get("retention")
    allo = r[r["oem"] == oem]
    d = allo[allo["dealer_code"].isin(codes)]
    zm = d["zm"].mode().iloc[0] if d["zm"].ne("").any() else ""
    rm = d["rm"].mode().iloc[0] if d["rm"].ne("").any() else ""
    closed, cur = _closed(d)
    y = _agg(closed)
    months = sorted(closed["month"].unique())
    last = months[-1] if months else None
    lm = _agg(closed[closed["month"] == last]) if last else None
    pp_all = pack.get("pc_penetration_month")
    pp_all = pp_all[pp_all["oem"] == oem] if len(pp_all) else pp_all
    pp = pp_all[pp_all["dealer_code"].map(G.parent_code).isin(codes)] if len(pp_all) else pp_all
    yr = (last or cur or "")[:4]
    pen_y = pen_l = None
    if len(pp):
        py = pp[pp["month"].str[:4] == yr]
        pen_y = ratio(py["new_nop"].sum(), py["retail"].sum()) if py["retail"].sum() else None
        pl = pp[pp["month"] == pp["month"].max()]
        pen_l = (ratio(pl["new_nop"].sum(), pl["retail"].sum()) if pl["retail"].sum() else None, pp["month"].max())
    period = f"{month_label(months[0])}-{month_label(last)}" if months else ""
    rep = Report(f"{name} - insurance renewal review",
                 f"{OEM_LABEL.get(oem, oem)} · {', '.join(codes)} · MIS as of {pack.asof.get('Private car', '')} · "
                 + ("internal review" if internal else "prepared by EIBL for the dealer meeting"))
    need = max(0, math.ceil(t["TOT"] * y["b"] - y["a"]))
    tiles = [(f"Renewal retention YTD ({period})", _p(y["r"]), f"{_n(y['a'])} of {_n(y['b'])} renewed · target {_p(t['TOT'])}"),
             ("First-year customers", _p(y["fy"]), f"{_n(y['FYa'])} of {_n(y['FYb'])} · target {_p(t['FY'])}"),
             ("Repeat customers (other year)", _p(y["oy"]), f"{_n(y['OYa'])} of {_n(y['OYb'])} · target {_p(t['OY'])}"),
             ("Renewals to reach target (YTD)", _n(need), "at the overall target")]
    if lm and lm["b"] >= A.MIN_BASE:
        tiles.append((f"Last closed month ({month_label(last)})", _p(lm["r"]), f"{_n(lm['a'])} of {_n(lm['b'])} renewed"))
    if pen_y is not None:
        tiles.append(("New-car penetration YTD", _pc(pen_y), f"policies sold with the car · {month_label(pen_l[1])}: {_pc(pen_l[0])}"))
    if oem == "VOLVO":
        nn = pack.get("volvo_ncb_month")
        if len(nn):
            nn = nn[nn["dealer_code"].astype(str).str.upper().apply(lambda c: any(k in c for k in codes))]
            nn = nn[(nn["month"] >= f"{yr}-01") & (nn["month"] <= (last or cur))]
            for flag, lab in (("NCB", "Customers with NCB"), ("Non-NCB", "Customers without NCB")):
                q = nn[nn["ncb"] == flag]
                if q["base"].sum():
                    tiles.append((f"{lab} - retention YTD", _p(ratio(q["ach"].sum(), q["base"].sum())), f"{_n(q['ach'].sum())} of {_n(q['base'].sum())}"))
    rep.kpis(tiles)
    cl_all, _ = _closed(allo)
    per = A.rollup(cl_all, ["dealer_code", "zm", "branch"]) if len(cl_all) else pd.DataFrame()
    branch = d["branch"].mode().iloc[0] if "branch" in d and d["branch"].ne("").any() else ""
    zone = per[per["zm"] == zm] if len(per) else per
    lvls = []                                              # branch (the car "region"), zone, national
    if branch: lvls.append((f"Branch {branch.title()}", per[per["branch"] == branch]))
    lvls += [("Zone", zone), ("National", per)]
    labels = tuple(l for l, _ in lvls)
    def brow(meas, you, num, den):
        vals = []
        for _, f in lvls: vals += list(_bench(f, num, den))
        return (meas, you, *vals, labels, False)
    bz, bn = _bench(zone, "Renewed", "Base"), _bench(per, "Renewed", "Base")
    rows = [brow("Renewal retention YTD", y["r"], "Renewed", "Base"), brow("First-year retention YTD", y["fy"], "FY ach", "FY base"),
            brow("Other-year retention YTD", y["oy"], "OY ach", "OY base")]
    if len(pp_all):
        pz = pp_all[pp_all["month"].str[:4] == yr].assign(dealer_code=lambda q: q["dealer_code"].map(G.parent_code))
        pz = pz.groupby("dealer_code", as_index=False)[["new_nop", "retail"]].sum()
        dmap = per.drop_duplicates("dealer_code").set_index("dealer_code") if len(per) else pd.DataFrame()
        pz["zm"] = pz["dealer_code"].map(dmap["zm"]) if len(dmap) else ""
        pz["branch"] = pz["dealer_code"].map(dmap["branch"]) if len(dmap) else ""
        # like for like: dealers in the renewal book that issued at least one policy this year (pending MISP codes excluded)
        pz = pz[pz["zm"].notna() & (pz["new_nop"] > 0)]
        vals = []
        for lab, _ in lvls:
            f = pz[pz["branch"] == branch] if lab.startswith("Branch") else pz[pz["zm"] == zm] if lab == "Zone" else pz
            vals += list(_bench(f, "new_nop", "retail"))
        rows.append(("New-car penetration YTD", pen_y, *vals, labels, True))
    msgs = []
    if y["b"]:
        gap = y["r"] - t["TOT"]
        msgs.append(f"Year to date **{_p(y['r'])}** of policies due were renewed - "
                    + (f"**{_pts(gap)}** above the {_p(t['TOT'])} target." if gap >= 0 else f"**{_n(need)} more renewals** would reach the {_p(t['TOT'])} target."))
    if bz[0] is not None:
        msgs.append(f"Zone average **{_p(bz[0])}** (median {_p(bz[1])}), national average **{_p(bn[0])}** - "
                    + ("ahead of the zone." if y["r"] >= bz[0] else "behind the zone."))
    if y["FYb"] and y["OYb"]:
        msgs.append(f"First-year customers renew at **{_p(y['fy'])}** against **{_p(y['oy'])}** for repeat customers"
                    + (" - the first renewal is where most customers are lost." if y["oy"] - y["fy"] > .10 else "."))
    if len(months) >= 6:
        a3 = _agg(closed[closed["month"].isin(months[-3:])])["r"]; b3 = _agg(closed[closed["month"].isin(months[-6:-3])])["r"]
        if a3 == a3 and b3 == b3:
            msgs.append(f"Last three months **{_p(a3)}** vs the three before **{_p(b3)}** "
                        f"({'improving' if a3 > b3 + .02 else 'slipping' if a3 < b3 - .02 else 'flat'}).")
    if pen_y is not None:
        msgs.append(f"New-car penetration **{_pc(pen_y)}** this year - every car sold without our policy is a renewal lost next year.")
    rep.bullets(msgs[:5], "Executive summary")
    rep.h("How you compare - " + (f"branch {branch.title()}, " if branch else "") + "zone and national (average and median)")
    rep.table(_bench_table(rows))
    rep.h("Month on month")
    if months:
        mm = A.rollup(closed, ["month"]).sort_values("month")
        series = {"Overall": list(mm["Ret %"]), "First year": list(mm["FY %"]), "Other year": list(mm["OY %"])}
        if len(pp):
            pm = pp.groupby("month")[["new_nop", "retail"]].sum()
            series["Penetration"] = [min(1.2, ratio(pm.at[m, "new_nop"], pm.at[m, "retail"])) if m in pm.index and pm.at[m, "retail"] else None
                                     for m in mm["month"]]
        rep.chart(ChartSpec("line", [month_label(m) for m in mm["month"]], series, "Renewal retention by month (closed months)", pct=True, target=t["TOT"]))
    mm_all = A.rollup(d[(d["month"].str[:4] == yr)], ["month"]).sort_values("month")
    rep.table(pd.DataFrame(_mom_rows(mm_all, cur)))
    if cur: rep.p(f"*{month_label(cur)} is still running: 'due' is the whole month's renewal list, so the rate rises as the month closes.")
    if len(codes) > 1:
        rows_ = []
        for c in codes:
            q = _agg(closed[closed["dealer_code"] == c])
            nm = d.loc[d["dealer_code"] == c, "dealer_name"].iloc[0] if (d["dealer_code"] == c).any() else ""
            rows_.append({"Code": c, "Dealer": nm, "Due YTD": _n(q["b"]), "Renewed": _n(q["a"]), "Retention": _p(q["r"]),
                          "First year": _p(q["fy"]), "Other year": _p(q["oy"])})
        rows_.append({"Code": f"Total {grp}", "Dealer": f"{len(codes)} codes", "Due YTD": _n(y["b"]), "Renewed": _n(y["a"]),
                      "Retention": _p(y["r"]), "First year": _p(y["fy"]), "Other year": _p(y["oy"])})
        rep.h("Each outlet, then the group")
        rep.table(pd.DataFrame(rows_))
    if len(pp):
        pm = pp.groupby("month")[["retail", "new_nop"]].sum().sort_index()
        pm = pm[pm.index.str[:4] == pm.index.max()[:4]]
        rep.h("New-car penetration by month")
        rep.table(pd.DataFrame([{"Month": month_label(m), "Cars sold": _n(v["retail"]), "Policies with the car": _n(v["new_nop"]),
                                 "Penetration": _pc(ratio(v["new_nop"], v["retail"]) if v["retail"] else None)} for m, v in pm.iterrows()]))
    oc = pack.get("volvo_open_cases")
    if oem == "VOLVO" and len(oc):
        o = oc[oc["dealer_code"].astype(str).apply(lambda q: any(c in q for c in codes))]
        if len(o):
            rep.h("Expired this month and not yet renewed")
            rep.kpis([("Policies", _n(len(o)), "to call back this week"), ("Premium at stake", A.inr(o["total_premium"].sum()), ""),
                      ("First-year among them", _n((o["segment"] == "FY").sum()), "")])
    if internal: _internal_block(pack, rep, codes, rm, zm)
    n_inv, amt = _payout_section(pack, rep, codes)
    acts = []
    if need: acts.append({"Action": f"Recover {min(need, max(5, round(need / 3)))} lapsed or due renewals in the next 30 days (call list from EIBL)",
                          "Owner": "Dealer insurance desk + EIBL", "By when": ""})
    if y["FYb"] and y["fy"] < t["FY"]:
        acts.append({"Action": f"Call every first-year customer 30-45 days before expiry - first year {_p(y['fy'])} towards {_p(t['FY'])}",
                     "Owner": "Dealer service advisors", "By when": ""})
    if pen_y is not None and pen_y < .7:
        acts.append({"Action": f"Offer our policy on every car delivered - penetration {_pc(pen_y)} to 70%+", "Owner": "Dealer sales team", "By when": ""})
    if n_inv: acts.append({"Action": f"Clear {n_inv} pending commission invoices ({A.inr(amt)})", "Owner": "Dealer accounts + EIBL", "By when": ""})
    _actions(rep, acts)
    return rep


# --------------------------------------------------------------------------- #
# Royal Enfield
# --------------------------------------------------------------------------- #
def _re_review(pack, code, internal=False) -> Report:
    s = pack.get("re_dealer_snapshot")
    hit = s[s["dealer_code"].astype(str).str.upper() == code.upper()]
    if hit.empty:
        return Report("Dealer review").p(f"No Royal Enfield data for dealer {code} in the current MIS.")
    x = hit.iloc[0]
    t = TARGETS["ROYAL ENFIELD"]
    has = lambda k: k in x and x[k] == x[k] and x[k] is not None
    rep = Report(f"{x['dealer_name']} - RESP renewal review",
                 f"Royal Enfield · {code} · region {x.get('region', '')} · {str(x.get('city', '') or '').title()} · MIS as of "
                 f"{pack.asof.get('Royal Enfield', '')} · " + ("internal review" if internal else "prepared by EIBL for the dealer meeting"))
    conv = ratio(x["mtd_actual"], x["mtd_target"]) if has("mtd_target") and x["mtd_target"] else None
    prev = ratio(x["mtd_actual_prev"], x["mtd_target_prev"]) if has("mtd_target_prev") and x["mtd_target_prev"] else None
    dm = pack.get("re_dailymail")
    dmr = dm[dm["dealer_code"] == x["dealer_code"]] if len(dm) else dm
    oyb, oya = (float(dmr["oy_base"].sum()), float(dmr["oy_ach"].sum())) if len(dmr) else (0, 0)
    fy = ratio(x["fy_ach"], x["fy_base"])
    pen = pack.get("re_penetration")
    pr = pen[pen["dealer_code"] == x["dealer_code"]] if len(pen) else pen
    p_ = pr.iloc[0] if len(pr) else None
    pen_y = ratio(p_["prog_ytd"], p_["veh_ytd"]) if p_ is not None and p_["veh_ytd"] else None
    add_y = ratio(p_["addon_ytd"], p_["prog_ytd"]) if p_ is not None and "addon_ytd" in p_ and p_["prog_ytd"] else None
    add_m = ratio(p_["addon_mtd"], p_["prog_mtd"]) if p_ is not None and "addon_mtd" in p_ and p_["prog_mtd"] else None
    from .mgmt import RS_DEALER, inr_short, re_earnings
    ea = re_earnings(pack, rate=RS_DEALER, dealer_code=x["dealer_code"])
    need = max(0, math.ceil(t["FY"] * x["fy_base"] - x["fy_ach"])) if x["fy_base"] else 0
    tiles = []
    if conv is not None:
        tiles.append(("Renewals on policies due so far", _p(conv), f"{_n(x['mtd_actual'])} of {_n(x['mtd_target'])} · same date last month {_p(prev)}"))
    tiles.append(("First year this month", _p(fy), f"{_n(x['fy_ach'])} of {_n(x['fy_base'])} due in the month · target {_p(t['FY'])}"))
    if oyb: tiles.append(("Repeat customers this month", _p(ratio(oya, oyb)), f"{_n(oya)} of {_n(oyb)} · target {_p(t['OY'])}"))
    tiles.append(("First-year renewals to target", _n(need), "this month"))
    if pen_y is not None: tiles.append(("New-bike penetration YTD", _pc(pen_y), f"{_n(p_['prog_ytd'])} policies on {_n(p_['veh_ytd'])} bikes"))
    if add_y is not None: tiles.append(("Add-on penetration YTD", _p(add_y), f"policies with an add-on cover · this month {_p(add_m)}"))
    if ea: tiles.append(("Earnings this month", inr_short(ea["earned"]), f"Rs {RS_DEALER}/renewal · at target {inr_short(ea['at_target'])}"))
    if has("tier"):
        tiles.append(("Incentive tier", f"+{int(x['tier'])} pts" if x["tier"] else "Not yet",
                      f"{_n(x['nop_to_next_tier'])} more first-year renewals to the next tier" if has("nop_to_next_tier") else ""))
    rep.kpis(tiles)
    reg, zone = s[s["region"] == x["region"]], s[s["zm"] == x["zm"]]
    labs = (f"Region {x['region']}", "Zone")
    rows = []
    if conv is not None:
        rows.append(("Renewals on policies due so far", conv, *_bench(reg, "mtd_actual", "mtd_target"), *_bench(zone, "mtd_actual", "mtd_target"), labs, False))
    rows.append(("First year this month (whole month)", fy, *_bench(reg, "fy_ach", "fy_base"), *_bench(zone, "fy_ach", "fy_base"), labs, False))
    if len(dm):
        dmz = dm.merge(s[["dealer_code", "zm"]].rename(columns={"zm": "zm_s"}), on="dealer_code", how="left")
        rows.append(("Repeat customers this month", ratio(oya, oyb) if oyb else None, *_bench(dmz[dmz["region"] == x["region"]], "oy_ach", "oy_base"),
                     *_bench(dmz[dmz["zm_s"] == x["zm"]], "oy_ach", "oy_base"), labs, False))
    if len(pen):
        pz_zone = pen[pen["dealer_code"].isin(zone["dealer_code"])]
        pz_reg = pen[pen["region"] == x["region"]]
        rows.append(("New-bike penetration YTD", pen_y, *_bench(pz_reg, "prog_ytd", "veh_ytd"), *_bench(pz_zone, "prog_ytd", "veh_ytd"), labs, True))
        if "addon_ytd" in pen:
            rows.append(("Add-on penetration YTD", add_y, *_bench(pz_reg, "addon_ytd", "prog_ytd"), *_bench(pz_zone, "addon_ytd", "prog_ytd"), labs, False))
    msgs = []
    if conv is not None:
        ra_, rmed = _bench(reg, "mtd_actual", "mtd_target")
        msgs.append(f"Of the policies that expired so far this month, **{_p(conv)}** renewed with you"
                    + (f" ({'up' if conv >= prev else 'down'} from {_p(prev)} on the same date last month)" if prev is not None else "")
                    + (f"; region {x['region']} average **{_p(ra_)}**, median {_p(rmed)}." if ra_ is not None else "."))
    msgs.append(f"First-year customers: **{_n(x['fy_ach'])}** of **{_n(x['fy_base'])}** due this month renewed so far; "
                f"**{_n(need)} more** reach the {_p(t['FY'])} first-year target.")
    if add_y is not None:
        msgs.append(f"**{_p(add_y)}** of your policies this year carry an add-on cover" + (" - offer the add-on on every policy." if add_y < .85 else "."))
    if ea and ea["left"]:
        msgs.append(f"At the 50% renewal target you would earn **{inr_short(ea['at_target'])}** this month; **{inr_short(ea['left'])}** is still to be earned.")
    if has("ai_conversions") and x["ai_conversions"]:
        msgs.append(f"EIBL AI calling converted **{_n(x['ai_conversions'])}** of your customers this month ({_n(x['ai_calls'])} calls).")
    rep.bullets(msgs[:5], "Executive summary")
    rep.h(f"How you compare - region {x['region']} and zone (average and median)")
    rep.table(_bench_table(rows))
    r = pack.get("retention")
    h = r[(r["oem"] == "ROYAL ENFIELD") & (r["dealer_code"] == x["dealer_code"])]
    rep.h("Month on month")
    if len(h):
        _, cur = _closed(h)
        mm = A.rollup(h, ["month"]).sort_values("month")
        cl = mm[mm["month"] != cur]
        if len(cl) >= 2:
            rep.chart(ChartSpec("line", [month_label(m) for m in cl["month"]],
                                {"Overall": list(cl["Ret %"]), "First year": list(cl["FY %"]), "Other year": list(cl["OY %"])},
                                "Renewal rate by month (closed months)", pct=True, target=t["FY"]))
        rep.table(pd.DataFrame(_mom_rows(mm, cur)))
        if cur: rep.p("*Running month: 'due' is the whole month's list, so the rate rises as the month closes.")
        if h["month"].nunique() <= 1:
            rep.p("Earlier months for this dealer are not in the monthly history file in Drive (it covers the North zone).")
    else:
        rep.p("No month-wise history for this dealer in the files in Drive.")
    if p_ is not None:
        rep.h("New-bike penetration and add-on covers")
        rep.table(pd.DataFrame([{"Period": lab, "Bikes sold": _n(p_[v]), "Policies issued": _n(p_[q]), "Penetration": _pc(ratio(p_[q], p_[v]) if p_[v] else None),
                                 "With add-on": _n(p_[a]) if a in p_ else "–", "Add-on penetration": _p(ratio(p_[a], p_[q]) if a in p_ and p_[q] else None)}
                                for lab, v, q, a in (("This month", "veh_mtd", "prog_mtd", "addon_mtd"), ("Year to date", "veh_ytd", "prog_ytd", "addon_ytd"))]))
        mix_cols = [c for c in ("essential", "advance", "premium", "bundled", "aloa", "no_addon") if c in p_ and p_[c] == p_[c]]
        tot = sum(p_[c] for c in mix_cols) if mix_cols else 0
        if tot:
            rep.table(pd.DataFrame([{"Cover": c.replace("_", " ").title().replace("Aloa", "ALOA"), "Policies": _n(p_[c]), "Share": _p(p_[c] / tot)}
                                    for c in mix_cols]), "Add-on cover mix")
    if internal: _internal_block(pack, rep, [x["dealer_code"]], x.get("rm", ""), x.get("zm", ""))
    n_inv, amt = _payout_section(pack, rep, [x["dealer_code"]])
    acts = [{"Action": f"Renew {max(need, 1)} more first-year customers this month - daily due list from EIBL", "Owner": "Dealer insurance desk + EIBL",
             "By when": "Month end"}]
    if add_y is not None and add_y < .85:
        acts.append({"Action": f"Offer an add-on cover on every policy - add-on penetration {_p(add_y)} to 85%+", "Owner": "Dealer sales / service", "By when": ""})
    acts.append({"Action": "Collect AI-calling consent at every delivery and service visit", "Owner": "Dealer sales / service", "By when": ""})
    if n_inv: acts.append({"Action": f"Clear {n_inv} pending commission invoices ({A.inr(amt)})", "Owner": "Dealer accounts + EIBL", "By when": ""})
    _actions(rep, acts)
    return rep


def dealer_deck(pack, code: str, internal: bool = False) -> Report:
    """Dealer review. internal=True adds RM / ZM, last visit and follow-up (the dashboard); False is dealer-facing."""
    code = str(code).strip().upper()
    s = pack.get("re_dealer_snapshot")
    if len(s) and (s["dealer_code"].astype(str).str.upper() == code).any():
        return _re_review(pack, code, internal)
    return _car_review(pack, code, internal)
