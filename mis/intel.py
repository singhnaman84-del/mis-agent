"""Executive intelligence layer: landing forecast, momentum, gap concentration, calendar and industry context.

Everything here is derived from the MIS already loaded, plus a small amount of fixed outside knowledge
(Indian festive calendar, motor-insurance renewal rules) that is stated as context, never mixed into the numbers.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from . import analytics as A
from .charts import ChartSpec
from .model import TARGETS

# Festive / holiday dates that move showroom footfall and renewal contactability (dates for 2026-27; regional variation applies).
CALENDAR = [
    ("2026-10-20", "Dussehra", "peak showroom footfall - new sales and penetration up, renewal follow-up slips"),
    ("2026-11-06", "Dhanteras", "highest vehicle-delivery day of the year - attach insurance at delivery"),
    ("2026-11-08", "Diwali", "dealer staff on leave around 7-10 Nov; front-load renewal calls before it"),
    ("2026-11-11", "Bhai Dooj", "post-Diwali lull - good window to clear expired cases"),
    ("2026-12-25", "Christmas / year-end", "year-end discounts lift sales; December renewals often slip to January"),
    ("2027-01-14", "Makar Sankranti / Pongal", "regional holiday (South / West) - plan calling around it"),
    ("2027-03-04", "Holi", "North holiday; also financial-year close pressure"),
    ("2027-03-31", "Financial-year close", "last day for FY targets and payouts"),
]

PLAYBOOK_COMMON = [
    "**NCB clock:** a lapsed policy keeps its No-Claim Bonus only if renewed within 90 days of expiry - chase expired cases inside 90 days first; they are the easiest to convert.",
    "**Contact window:** renewal conversion is highest when the customer is reached 30-45 days before expiry; cases reached after expiry convert far less often.",
    "**Pareto rule:** a small share of dealers carries most of the gap - fix the top few before spreading effort across the long tail.",
]
PLAYBOOK = {
    "ROYAL ENFIELD": [
        "**Third-party cover is mandatory** under the Motor Vehicles Act - lapsed two-wheeler owners are riding uninsured; lead with that, not with price.",
        "**First-year renewals are the lever:** other-year customers already renew at a high rate; the target is missed or met on first-year conversion.",
        "**AI calling** already supplies a large share of first-year renewals - feed it the full due list early in the month.",
    ],
    "CAR": [
        "**Own-damage renewal competes with the open market** - renewal price and claim-service experience decide retention; quote early before aggregators reach the customer.",
        "**Service-visit capture:** match workshop visits against due-for-renewal lists so the policy is offered at the service desk.",
    ],
}


def _asof(pack, oem):
    book = "Royal Enfield" if oem == "ROYAL ENFIELD" else "Private car"
    try:
        return dt.date.fromisoformat(str(pack.asof.get(book, ""))[:10])
    except ValueError:
        return None


def landing(pack, oem, zm=None, rm=None) -> dict | None:
    """Month-end landing at the current run rate vs target and last month's close."""
    from .views import summary
    if oem in (None, "OVERALL"): return None
    asof = _asof(pack, oem)
    if not asof: return None
    s = summary(pack, oem, zm, rm)
    m = s["mtd"]
    base, ren = float(m.get("Base") or 0), float(m.get("Renewed") or 0)
    if base <= 0: return None
    dim = (dt.date(asof.year + (asof.month == 12), asof.month % 12 + 1, 1) - dt.timedelta(days=1)).day
    proj = min(base, ren / max(1, asof.day) * dim)
    t = float(TARGETS.get(oem, TARGETS["SKODA"])["TOT"])
    lm = s["lm"].get("Ret %") if s.get("lm") else None
    lm = float(lm) if lm is not None and lm == lm else None
    return {"to_date": ren / base, "proj": proj / base, "target": t, "lm": lm, "day": asof.day, "dim": dim,
            "short": max(0.0, t * base - proj), "status": "g" if proj / base >= t else "a" if proj / base >= t - .05 else "r"}


def momentum(months: pd.DataFrame) -> dict | None:
    """Slope of the overall % over the last closed months (pts per month) and the next-month extrapolation."""
    if months is None or not len(months) or "Overall %" not in months: return None
    d = months[~months["Month"].astype(str).str.contains("to date|MTD", case=False)]
    y = d["Overall %"].astype(float).dropna().tail(4).to_numpy()
    if len(y) < 3: return None
    slope = float(np.polyfit(np.arange(len(y)), y, 1)[0])
    best = d.loc[d["Overall %"].astype(float).idxmax()]
    return {"slope": slope, "last": float(y[-1]), "next": float(y[-1] + slope), "n": len(y),
            "best": (str(best["Month"]), float(best["Overall %"])),
            "status": "g" if slope > .005 else "r" if slope < -.005 else "a"}


def concentration(score: pd.DataFrame) -> dict | None:
    """How concentrated the renewal gap is across the managers / units in the scorecard."""
    if score is None or not len(score) or "NOP to target" not in score: return None
    g = score[["Name", "NOP to target"]].copy()
    g["NOP to target"] = pd.to_numeric(g["NOP to target"], errors="coerce").fillna(0).clip(lower=0)
    g = g[g["NOP to target"] > 0].sort_values("NOP to target", ascending=False)
    tot = g["NOP to target"].sum()
    if tot <= 0 or len(g) < 2: return None
    k = min(3, len(g))
    share = float(g["NOP to target"].head(k).sum() / tot)
    cum = (g["NOP to target"].cumsum() / tot).tolist()
    top = g.head(12)
    chart = ChartSpec("hbar", [str(n).title()[:22] for n in top["Name"]],
                      {"Renewals to target": top["NOP to target"].astype(int).tolist()},
                      title=f"Where the gap sits - top {k} hold {share:.0%} of {int(tot):,} renewals")
    return {"k": k, "n": len(g), "share": share, "total": float(tot), "chart": chart,
            "names": [str(n).title() for n in g["Name"].head(k)], "cum": cum,
            "status": "r" if share >= .6 else "a"}


def calendar(asof: dt.date | None, days=60) -> list[str]:
    if not asof: return []
    out = []
    for d, name, why in CALENDAR:
        dd = dt.date.fromisoformat(d)
        if 0 <= (dd - asof).days <= days:
            out.append(f"**{dd:%d %b} - {name}** (in {(dd - asof).days} days): {why}.")
    return out


def intel(pack, oem, zm=None, rm=None, v: dict | None = None) -> dict:
    """{'tiles': [(title, value, sub, status)], 'chart': ChartSpec|None, 'calendar': [...], 'playbook': [...], 'so_what': str}"""
    v = v or {}
    tiles, so = [], []
    L = landing(pack, oem, zm, rm)
    if L:
        tiles.append(("Month-end landing (run rate)", A.pct(L["proj"]),
                      f"to date {A.pct(L['to_date'])} · target {A.pct(L['target'])}"
                      + (f" · last month {A.pct(L['lm'])}" if L["lm"] is not None else ""), L["status"]))
        if L["status"] != "g":
            so.append(f"At today's run rate the month lands at {A.pct(L['proj'])} vs {A.pct(L['target'])} - "
                      f"about {A.n(L['short'])} renewals short.")
    if oem == "OVERALL":
        from .model import OEM_LABEL
        for o in ("ROYAL ENFIELD", "SKODA", "VOLKSWAGEN", "AUDI", "VOLVO"):
            try: Lo = landing(pack, o, zm, rm)
            except Exception: Lo = None
            if Lo:
                tiles.append((f"{OEM_LABEL.get(o, o)} - month-end landing", A.pct(Lo["proj"]),
                              f"target {A.pct(Lo['target'])} · to date {A.pct(Lo['to_date'])}", Lo["status"]))
                if Lo["status"] == "r": so.append(f"{OEM_LABEL.get(o, o)} is heading for {A.pct(Lo['proj'])} vs {A.pct(Lo['target'])}.")
    M = momentum(v.get("months"))
    if M:
        tiles.append(("Momentum (last {} closed months)".format(M["n"]), f"{M['slope'] * 100:+.1f} pts/month",
                      f"last close {A.pct(M['last'])} · best {M['best'][0]} {A.pct(M['best'][1])}", M["status"]))
        if M["status"] == "r": so.append(f"Retention has been sliding about {abs(M['slope']) * 100:.1f} pts a month.")
    Cn = concentration(v.get("score"))
    if Cn:
        tiles.append((f"Gap concentration", f"{Cn['share']:.0%} in top {Cn['k']}",
                      f"of {A.n(Cn['total'])} renewals: " + ", ".join(Cn["names"]), Cn["status"]))
        so.append(f"Fix {', '.join(Cn['names'])} first - they hold {Cn['share']:.0%} of the gap.")
    asof = _asof(pack, oem if oem != "OVERALL" else "SKODA") or _asof(pack, "ROYAL ENFIELD")
    cal = calendar(asof)
    if cal:
        nxt = cal[0].split("**")[1]
        tiles.append(("Next calendar event", nxt.split(" - ")[1], nxt.split(" - ")[0] + " · plan calling around it", "a"))
    pb = PLAYBOOK_COMMON + (PLAYBOOK["ROYAL ENFIELD"] if oem == "ROYAL ENFIELD" else
                            PLAYBOOK["CAR"] if oem != "OVERALL" else PLAYBOOK["ROYAL ENFIELD"][:1] + PLAYBOOK["CAR"][:1])
    return {"tiles": tiles, "chart": Cn["chart"] if Cn else None, "calendar": cal, "playbook": pb,
            "so_what": " ".join(so), "landing": L, "momentum": M}


def cockpit(pack, oem, zm=None, rm=None) -> dict:
    """intel() for a scope, computing the overview itself (so callers can memoise on plain arguments)."""
    from .mgmt import overview
    return intel(pack, oem, zm, rm, overview(pack, oem, zm, rm))
