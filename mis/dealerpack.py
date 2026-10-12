"""Dealer meeting deck - the pack a dealer principal sees.

Principles
- Lead with the year to date on CLOSED months (a 2-policy month or a half-finished running month never headlines).
- The running month is shown separately, labelled "to date", with its basis stated.
- Retention and penetration side by side; plain-language key messages; pre-filled, numeric actions.
- Clubbed groups get one combined deck (each code, then the group total).
- Dealer-facing only: anonymised peers; the dealer's own unpaid commission invoices; no RM visits, no defaulter labels,
  no customer data.
"""
from __future__ import annotations

import math
import statistics

import pandas as pd

from . import analytics as A
from . import groups as G
from .charts import ChartSpec
from .model import OEM_LABEL, TARGETS
from .report import Report
from .util import month_label, ratio


def _p(x):
    return "–" if x is None or x != x else f"{x * 100:.1f}%"


def _n(x):
    try: return f"{float(x):,.0f}"
    except (TypeError, ValueError): return "–"


def _pts(x):
    return "–" if x is None or x != x else f"{x * 100:+.1f} pts"


def _closed(d: pd.DataFrame):
    """(closed-month frame for this year, running month key)."""
    if d.empty: return d, None
    last, _, cur = A.months(d)
    top = last or cur
    year = (top or "")[:4]
    closed = d[(~d["is_mtd"].astype(bool)) & (d["month"].str[:4] == year)]
    return closed, cur


def _agg(d: pd.DataFrame) -> dict:
    out = {}
    for seg in ("FY", "OY"):
        x = d[d["segment"] == seg]
        out[f"{seg}b"], out[f"{seg}a"] = float(x["base"].sum()), float(x["ach"].sum())
    out["b"], out["a"] = out["FYb"] + out["OYb"], out["FYa"] + out["OYa"]
    out["r"], out["fy"], out["oy"] = ratio(out["a"], out["b"]), ratio(out["FYa"], out["FYb"]), ratio(out["OYa"], out["OYb"])
    return out


def _codes_for(pack, code: str):
    """(oem, codes, display name, group name or None) for a car dealer code."""
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
    """The dealer's own unpaid commission invoices (amount, status, what they need to do). No RM / defaulter labels."""
    from .mailer import action_for, unpaid_for
    rows = []
    for c in codes:
        u = unpaid_for(pack, str(c).upper())
        for x in u.itertuples():
            rows.append({"Code": c, "Invoice": x.inv, "Month": month_label(x.month) if isinstance(x.month, str) else "",
                         "Amount": float(x.value or 0), "Status": x.status, "Action needed": action_for(x.status)})
    rep.h("Your commission (payout) status")
    if not rows:
        rep.p("No commission invoices are pending - all paid.")
        return
    t = pd.DataFrame(rows).sort_values(["Month", "Invoice"])
    sched = t["Action needed"].str.startswith("No action")
    rep.kpis([("Unpaid invoices", _n(len(t)), "commission pending"), ("Amount pending", A.inr(t["Amount"].sum()), "base value"),
              ("Waiting on you", _n(int((~sched).sum())), "invoices that need an action from the dealer")])
    show = t.assign(Amount=t["Amount"].map(A.inr))
    if len(codes) <= 1: show = show.drop(columns=["Code"])
    rep.table(show.head(40))
    if len(t) > 40: rep.p(f"... and {len(t) - 40} more invoices - full list from your RM.")


# --------------------------------------------------------------------------- #
# private car (Audi / Skoda / VW / Volvo)
# --------------------------------------------------------------------------- #
def _car_peers(pack, oem, zm, codes, me_base):
    r = pack.get("retention")
    d = r[(r["oem"] == oem) & (r["zm"] == zm) & ~r["dealer_code"].isin(codes)]
    closed, _ = _closed(d)
    if closed.empty: return None
    g = A.rollup(closed, ["dealer_code"])
    g = g[g["Base"] >= A.MIN_BASE]
    pool, band = g, "the whole zone"
    for bd, lab in ((.30, "a similar renewal base (±30%)"), (.50, "a similar renewal base (±50%)")):
        x = g[(g["Base"] - me_base).abs() <= bd * max(me_base, 1)]
        if len(x) >= 4:
            pool, band = x, lab
            break
    if len(pool) < 3: return None
    return {"n": len(pool), "median": statistics.median(pool["Ret %"]), "best": pool["Ret %"].max(),
            "q75": pool["Ret %"].quantile(.75), "band": band, "vals": list(pool["Ret %"])}


def _car_deck(pack, code) -> Report:
    who = _codes_for(pack, code)
    if not who:
        return Report("Dealer meeting deck").p(f"No renewal data for dealer {code} in the current MIS.")
    oem, codes, name, grp = who
    t = TARGETS.get(oem, TARGETS["SKODA"])
    r = pack.get("retention")
    d = r[(r["oem"] == oem) & r["dealer_code"].isin(codes)]
    zm = d["zm"].mode().iloc[0] if d["zm"].ne("").any() else ""
    closed, cur = _closed(d)
    y = _agg(closed)
    months = sorted(closed["month"].unique())
    last = months[-1] if months else None
    lm = _agg(closed[closed["month"] == last]) if last else None
    run = _agg(d[d["month"] == cur]) if cur else None
    # penetration (sub-outlet codes roll up to the parent code)
    pp = pack.get("pc_penetration_month")
    pp = pp[(pp["oem"] == oem) & pp["dealer_code"].map(G.parent_code).isin(codes)] if len(pp) else pp
    pen_y = pen_l = None
    if len(pp):
        py = pp[pp["month"].str[:4] == (last or cur or "")[:4]]
        pen_y = ratio(py["new_nop"].sum(), py["retail"].sum()) if py["retail"].sum() else None
        pl = pp[pp["month"] == pp["month"].max()]
        pen_l = (ratio(pl["new_nop"].sum(), pl["retail"].sum()) if pl["retail"].sum() else None, pp["month"].max())
    period = f"{month_label(months[0])}-{month_label(last)}" if months else ""
    sub = (f"{OEM_LABEL.get(oem, oem)} · {', '.join(codes)} · MIS as of {pack.asof.get('Private car', '')} · "
           "prepared by EIBL for the dealer meeting")
    rep = Report(f"{name} - insurance renewal review", sub)
    need = max(0, math.ceil(t["TOT"] * y["b"] - y["a"]))
    tiles = [(f"Renewal retention YTD ({period})", _p(y["r"]), f"{_n(y['a'])} of {_n(y['b'])} renewed · target {_p(t['TOT'])}"),
             ("First-year customers", _p(y["fy"]), f"{_n(y['FYa'])} of {_n(y['FYb'])} · target {_p(t['FY'])}"),
             ("Repeat customers (other year)", _p(y["oy"]), f"{_n(y['OYa'])} of {_n(y['OYb'])} · target {_p(t['OY'])}"),
             ("Renewals to reach target (YTD)", _n(need), "at the overall target")]
    if lm and lm["b"] >= A.MIN_BASE:
        tiles.append((f"Last closed month ({month_label(last)})", _p(lm["r"]), f"{_n(lm['a'])} of {_n(lm['b'])} renewed"))
    if pen_y is not None:
        tiles.append((f"New-car penetration YTD", _p(pen_y), f"policies sold with the car · {month_label(pen_l[1])}: {_p(pen_l[0])}"))
    if oem == "VOLVO":
        nn = pack.get("volvo_ncb_month")
        if len(nn):
            nn = nn[nn["dealer_code"].astype(str).str.upper().apply(lambda c: any(k in c for k in codes))]
            nn = nn[(nn["month"] >= f"{(last or cur)[:4]}-01") & (nn["month"] <= (last or cur))]
            for flag, lab in (("NCB", "Customers with NCB"), ("Non-NCB", "Customers without NCB")):
                q = nn[nn["ncb"] == flag]
                if q["base"].sum():
                    tiles.append((f"{lab} - retention YTD", _p(ratio(q["ach"].sum(), q["base"].sum())), f"{_n(q['ach'].sum())} of {_n(q['base'].sum())}"))
    rep.kpis(tiles)
    # key messages
    msgs = []
    if y["b"]:
        gap = y["r"] - t["TOT"]
        msgs.append(f"Year to date **{_p(y['r'])}** of policies due were renewed with you - "
                    + (f"**{_pts(gap)}** above the {_p(t['TOT'])} target." if gap >= 0 else
                       f"**{_n(need)} more renewals** would have put you at the {_p(t['TOT'])} target."))
    if y["FYb"] and y["OYb"]:
        msgs.append(f"First-year customers renew at **{_p(y['fy'])}** against **{_p(y['oy'])}** for repeat customers - "
                    + ("the first renewal is where most customers are lost." if y["oy"] - y["fy"] > .10 else
                       "first-year and repeat customers renew at similar rates."))
    peer = _car_peers(pack, oem, zm, codes, y["b"])
    if peer:
        better = sum(v > y["r"] for v in peer["vals"])
        msgs.append(f"Among {peer['n']} {OEM_LABEL.get(oem, oem)} dealers in your zone with {peer['band']}, the median is "
                    f"**{_p(peer['median'])}** and the best **{_p(peer['best'])}**; {better} of them retain more than you.")
    if len(months) >= 6:
        a3 = _agg(closed[closed["month"].isin(months[-3:])])["r"]; b3 = _agg(closed[closed["month"].isin(months[-6:-3])])["r"]
        if a3 == a3 and b3 == b3:
            msgs.append(f"Last three months **{_p(a3)}** vs the three before **{_p(b3)}** "
                        f"({'improving' if a3 > b3 + .02 else 'slipping' if a3 < b3 - .02 else 'flat'}).")
    if pen_y is not None:
        msgs.append(f"New-car penetration (policies sold with the car) is **{_p(pen_y)}** this year - every car sold without "
                    "our policy is a first-year renewal you will not see next year.")
    rep.bullets(msgs[:4], "Key messages")
    # trend: closed months only; running month in the table, labelled
    if months:
        mm = A.rollup(closed, ["month"]).sort_values("month")
        series = {"Overall": list(mm["Ret %"]), "First year": list(mm["FY %"]), "Other year": list(mm["OY %"])}
        if len(pp):
            pm = pp.groupby("month")[["new_nop", "retail"]].sum()
            series["Penetration"] = [ratio(pm.at[m, "new_nop"], pm.at[m, "retail"]) if m in pm.index and pm.at[m, "retail"] else None
                                     for m in mm["month"]]
        rep.h("Month by month")
        rep.chart(ChartSpec("line", [month_label(m) for m in mm["month"]], series,
                            "Renewal retention by month (closed months)", pct=True, target=t["TOT"]))
        rows = [{"Month": month_label(x["month"]), "Due": _n(x["Base"]), "Renewed": _n(x["Renewed"]), "Retention": _p(x["Ret %"]),
                 "First year": _p(x["FY %"]), "Other year": _p(x["OY %"])} for _, x in mm.iterrows()]
        if run and run["b"]:
            rows.append({"Month": f"{month_label(cur)} to date*", "Due": _n(run["b"]), "Renewed": _n(run["a"]), "Retention": _p(run["r"]),
                         "First year": _p(run["fy"]), "Other year": _p(run["oy"])})
        rep.table(pd.DataFrame(rows))
        if run and run["b"]:
            rep.p(f"*{month_label(cur)} is still running: 'due' is the whole month's renewal list, so the rate rises as the month closes.")
    # group: each code, then the total
    if len(codes) > 1:
        rows = []
        for c in codes:
            x = _agg(closed[closed["dealer_code"] == c])
            nm = d.loc[d["dealer_code"] == c, "dealer_name"].iloc[0] if (d["dealer_code"] == c).any() else ""
            rows.append({"Code": c, "Dealer": nm, "Due YTD": _n(x["b"]), "Renewed": _n(x["a"]), "Retention": _p(x["r"]),
                         "First year": _p(x["fy"]), "Other year": _p(x["oy"])})
        rows.append({"Code": f"Σ {grp}", "Dealer": f"{len(codes)} codes", "Due YTD": _n(y["b"]), "Renewed": _n(y["a"]),
                     "Retention": _p(y["r"]), "First year": _p(y["fy"]), "Other year": _p(y["oy"])})
        rep.h("Each outlet, then the group")
        rep.table(pd.DataFrame(rows))
    if peer:
        rep.h("How you compare (anonymised)")
        rep.table(pd.DataFrame([{"": "Renewal retention YTD", "You": _p(y["r"]), "Peer median": _p(peer["median"]),
                                 "Top quarter": _p(peer["q75"]), "Best": _p(peer["best"]), "Peers": f"{peer['n']} dealers, {peer['band']}"}]))
    if len(pp):
        pm = pp.groupby("month")[["retail", "new_nop"]].sum().sort_index()
        pm = pm[pm.index.str[:4] == pm.index.max()[:4]]
        rep.h("New-car penetration")
        rep.table(pd.DataFrame([{"Month": month_label(m), "Cars sold": _n(v["retail"]), "Policies with the car": _n(v["new_nop"]),
                                 "Penetration": _p(ratio(v["new_nop"], v["retail"]) if v["retail"] else None)} for m, v in pm.iterrows()]))
    oc = pack.get("volvo_open_cases")
    if oem == "VOLVO" and len(oc):
        o = oc[oc["dealer_code"].astype(str).apply(lambda x: any(c in x for c in codes))]
        if len(o):
            rep.h("Expired this month and not yet renewed")
            rep.kpis([("Policies", _n(len(o)), "to call back this week"), ("Premium at stake", A.inr(o["total_premium"].sum()), ""),
                      ("First-year among them", _n((o["segment"] == "FY").sum()), "")])
    _payout_section(pack, rep, codes)
    # actions: concrete numbers, then blanks for the meeting
    acts = []
    if need: acts.append({"#": 1, "Action": f"Recover {min(need, max(5, round(need / 3)))} lapsed or due renewals in the next 30 days "
                                            "(call list shared by EIBL)", "Owner": "Dealer insurance desk + EIBL", "By when": ""})
    if y["FYb"] and y["fy"] < t["FY"]:
        acts.append({"#": len(acts) + 1, "Action": f"Call every first-year customer 30-45 days before expiry - lift first-year from "
                                                   f"{_p(y['fy'])} towards {_p(t['FY'])}", "Owner": "Dealer service advisors", "By when": ""})
    if pen_y is not None and pen_y < .7:
        acts.append({"#": len(acts) + 1, "Action": f"Offer our policy on every car delivered - penetration from {_p(pen_y)} to 70%+",
                     "Owner": "Dealer sales team", "By when": ""})
    while len(acts) < 5: acts.append({"#": len(acts) + 1, "Action": "", "Owner": "", "By when": ""})
    rep.h("Agreed actions")
    rep.table(pd.DataFrame(acts))
    return rep


# --------------------------------------------------------------------------- #
# Royal Enfield
# --------------------------------------------------------------------------- #
def _re_deck(pack, code) -> Report:
    s = pack.get("re_dealer_snapshot")
    hit = s[s["dealer_code"].astype(str).str.upper() == code.upper()]
    if hit.empty:
        return Report("Dealer meeting deck").p(f"No Royal Enfield data for dealer {code} in the current MIS.")
    x = hit.iloc[0]
    t = TARGETS["ROYAL ENFIELD"]
    asof = pack.asof.get("Royal Enfield", "")
    rep = Report(f"{x['dealer_name']} - RESP renewal review",
                 f"Royal Enfield · {code} · {x.get('city', '') or ''} · MIS as of {asof} · prepared by EIBL for the dealer meeting")
    has = lambda k: k in x and x[k] == x[k] and x[k] is not None
    conv = ratio(x["mtd_actual"], x["mtd_target"]) if has("mtd_target") and x["mtd_target"] else None
    prev = ratio(x["mtd_actual_prev"], x["mtd_target_prev"]) if has("mtd_target_prev") and x["mtd_target_prev"] else None
    dm = pack.get("re_dailymail")
    dmr = dm[dm["dealer_code"] == x["dealer_code"]] if len(dm) else dm
    oyb, oya = (float(dmr["oy_base"].sum()), float(dmr["oy_ach"].sum())) if len(dmr) else (0, 0)
    fy = ratio(x["fy_ach"], x["fy_base"])
    tiles = []
    if conv is not None:
        tiles.append(("Renewals on policies due so far", _p(conv), f"{_n(x['mtd_actual'])} of {_n(x['mtd_target'])} · "
                      + (f"same date last month {_p(prev)}" if prev is not None else "this month")))
    tiles.append(("First-year this month", _p(fy), f"{_n(x['fy_ach'])} of {_n(x['fy_base'])} due in the month · target {_p(t['FY'])}"))
    if oyb: tiles.append(("Repeat customers this month", _p(ratio(oya, oyb)), f"{_n(oya)} of {_n(oyb)} · target {_p(t['OY'])}"))
    need = max(0, math.ceil(t["FY"] * x["fy_base"] - x["fy_ach"])) if x["fy_base"] else 0
    tiles.append(("First-year renewals to target", _n(need), "this month"))
    pen = pack.get("re_penetration")
    pr = pen[pen["dealer_code"] == x["dealer_code"]] if len(pen) else pen
    if len(pr):
        p_ = pr.iloc[0]
        pvy = ratio(p_["prog_ytd"], p_["veh_ytd"]) if p_["veh_ytd"] else None
        tiles.append(("New-bike penetration YTD", _p(pvy) if pvy is None or pvy <= 1 else "100%+",
                      f"{_n(p_['prog_ytd'])} policies on {_n(p_['veh_ytd'])} bikes · this month {(lambda q: _p(q) if q is None or q <= 1 else '100%+')(ratio(p_['prog_mtd'], p_['veh_mtd']) if p_['veh_mtd'] else None)}"))
    if len(pr) and "addon_ytd" in pr and pr.iloc[0]["prog_ytd"]:
        p_ = pr.iloc[0]
        tiles.append(("Add-on penetration YTD", _p(ratio(p_["addon_ytd"], p_["prog_ytd"])),
                      f"{_n(p_['addon_ytd'])} of {_n(p_['prog_ytd'])} policies with an add-on cover"
                      + (f" · this month {_p(ratio(p_['addon_mtd'], p_['prog_mtd']))}" if p_["prog_mtd"] else "")))
    from .mgmt import RS_DEALER, inr_short, re_earnings
    ea = re_earnings(pack, rate=RS_DEALER, dealer_code=x["dealer_code"])
    if ea:
        tiles.append(("Your earnings this month", inr_short(ea["earned"]),
                      f"Rs {RS_DEALER} per renewal · at target {inr_short(ea['at_target'])} · {inr_short(ea['left'])} still to earn"))
    if has("tier"):
        tiles.append(("Incentive tier", f"+{int(x['tier'])} pts" if x["tier"] else "Not yet",
                      f"{_n(x['nop_to_next_tier'])} more first-year renewals to the next tier" if has("nop_to_next_tier") else ""))
    rep.kpis(tiles)
    msgs = []
    if conv is not None:
        msgs.append(f"Of the policies that have expired so far this month, **{_p(conv)}** renewed with you"
                    + (f" - {'up' if conv >= prev else 'down'} from **{_p(prev)}** on the same date last month." if prev is not None else "."))
    msgs.append(f"First-year customers: **{_n(x['fy_ach'])}** of **{_n(x['fy_base'])}** due this month renewed so far; "
                f"**{_n(need)} more** reach the {_p(t['FY'])} first-year target.")
    if has("ai_calls") and x["ai_calls"] and has("ai_conversions") and x["ai_conversions"]:
        msgs.append(f"EIBL's AI calling made **{_n(x['ai_calls'])}** calls for your customers this month and converted "
                    f"**{_n(x.get('ai_conversions', 0))}** - consent from more customers widens this.")
    if len(pr):
        p_ = pr.iloc[0]
        pv = ratio(p_["prog_ytd"], p_["veh_ytd"]) if p_["veh_ytd"] else None
        if pv is not None and pv <= 1:
            msgs.append(f"**{_p(pv)}** of bikes sold this year left with our policy - each one becomes a first-year renewal next year.")
        elif pv is not None:
            msgs.append(f"**{_n(p_['prog_ytd'])}** policies issued this year against **{_n(p_['veh_ytd'])}** bikes sold "
                        "(the count includes policies for bikes sold earlier) - keep offering the policy on every delivery.")
    if len(pr) and "addon_ytd" in pr and pr.iloc[0]["prog_ytd"]:
        p_ = pr.iloc[0]
        ad = ratio(p_["addon_ytd"], p_["prog_ytd"])
        nat = pen[["addon_ytd", "prog_ytd"]].sum()
        msgs.append(f"**{_p(ad)}** of your policies this year carry an add-on cover (all Royal Enfield dealers: "
                    f"**{_p(ratio(nat['addon_ytd'], nat['prog_ytd']))}**)" + (" - offer the add-on on every policy." if ad < .85 else "."))
    if ea and ea["left"]:
        msgs.append(f"At the 50% renewal target you would earn **{inr_short(ea['at_target'])}** this month; "
                    f"**{inr_short(ea['left'])}** is still to be earned (Rs {RS_DEALER} per renewed policy).")
    rep.bullets(msgs[:6], "Key messages")
    # peers: same zone, similar base, on the due-so-far conversion
    if conv is not None and "mtd_target" in s:
        z = s[(s["zm"] == x["zm"]) & (s["dealer_code"] != x["dealer_code"]) & (s["mtd_target"] >= A.MIN_BASE)].copy()
        z["conv"] = z["mtd_actual"] / z["mtd_target"]
        pool, band = z, "the whole zone"
        for bd, lab in ((.30, "a similar base (±30%)"), (.50, "a similar base (±50%)")):
            q = z[(z["mtd_target"] - x["mtd_target"]).abs() <= bd * max(x["mtd_target"], 1)]
            if len(q) >= 4: pool, band = q, lab; break
        if len(pool) >= 3:
            rep.h("How you compare (anonymised)")
            rep.table(pd.DataFrame([{"": "Renewals on policies due so far", "You": _p(conv), "Peer median": _p(pool["conv"].median()),
                                     "Top quarter": _p(pool["conv"].quantile(.75)), "Best": _p(pool["conv"].max()),
                                     "Peers": f"{len(pool)} Royal Enfield dealers in your zone, {band}"}]))
    # monthly history when it exists for this dealer
    r = pack.get("retention")
    h = r[(r["oem"] == "ROYAL ENFIELD") & (r["dealer_code"] == x["dealer_code"])]
    closed, _ = _closed(h)
    if closed["month"].nunique() >= 2:
        mm = A.rollup(closed, ["month"]).sort_values("month")
        rep.h("Month by month")
        rep.chart(ChartSpec("line", [month_label(m) for m in mm["month"]], {"First year": list(mm["FY %"]), "Other year": list(mm["OY %"])},
                            "Renewal rate by month (closed months)", pct=True, target=t["FY"]))
    if len(pr):
        p_ = pr.iloc[0]
        rep.h("New-bike penetration")
        rep.table(pd.DataFrame([{"Period": lab, "Bikes sold": _n(p_[v]), "Policies issued": _n(p_[pp_]),
                                 "Penetration": (lambda q: _p(q) if q is None or q <= 1 else "100%+")(ratio(p_[pp_], p_[v]) if p_[v] else None)}
                                for lab, v, pp_ in (("This month", "veh_mtd", "prog_mtd"), ("Year to date", "veh_ytd", "prog_ytd"))]))
    _payout_section(pack, rep, [x["dealer_code"]])
    acts = [{"#": 1, "Action": f"Renew {max(need, 1)} more first-year customers this month - daily due list from EIBL",
             "Owner": "Dealer insurance desk + EIBL", "By when": "Month end"},
            {"#": 2, "Action": "Collect AI-calling consent at every delivery and service visit", "Owner": "Dealer sales / service", "By when": ""}]
    while len(acts) < 5: acts.append({"#": len(acts) + 1, "Action": "", "Owner": "", "By when": ""})
    rep.h("Agreed actions")
    rep.table(pd.DataFrame(acts))
    return rep


def dealer_deck(pack, code: str) -> Report:
    code = str(code).strip().upper()
    s = pack.get("re_dealer_snapshot")
    if len(s) and (s["dealer_code"].astype(str).str.upper() == code).any():
        return _re_deck(pack, code)
    return _car_deck(pack, code)
