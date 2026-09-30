"""MIS workbook -> tidy DataFrames.

One loader per file type. Every loader is defensive: a missing sheet or column degrades to an
empty frame plus a data-quality note, never an exception, so one bad file never takes the app down.

Customer-level identifiers (names, policy/chassis/engine/registration numbers, address, PAN, GSTIN,
UTR numbers, phone/e-mail) are never loaded.
"""
from __future__ import annotations

import datetime as dt
import glob
import os
import re
from dataclasses import dataclass, field

import pandas as pd

from .util import (code, date_from_name, drop_total_rows, excel_date, ffill, find_row, grid, iter_sheet, month_key,
                   num, person, s, sheet_names)

# --------------------------------------------------------------------------- #
# file catalogue
# --------------------------------------------------------------------------- #
FILE_TYPES = {
    # key: (regex on file name, human label, book)
    "pc_renewal": (r"YTD-MTD Dealer And RM & ZM Wise Renewal Report", "Audi/Skoda/VW YTD-MTD renewal report", "Private car"),
    "volvo_summary": (r"VOLVO Retention Summary", "Volvo retention summary (FY/OY by month)", "Private car"),
    "volvo_conversion": (r"Volvo Conversion.*Not Renewed", "Volvo conversion + not-renewed cases", "Private car"),
    "volvo_daily": (r"VOLVO Daily Renewal Retention", "Volvo daily renewal retention workbook", "Private car"),
    "savwipl_pen": (r"SAVWIPL Penetration", "SAVWIPL new-car penetration MIS", "Private car"),
    "savwipl_utr": (r"SAVWIPL.*UTR", "SAVWIPL & Volvo payout / UTR status", "Private car"),
    "re_renewal": (r"RESP Renewal Report", "RE renewal report (region, dealer MTD, EDME 360)", "Royal Enfield"),
    "re_ai": (r"Royal Enfield AI Calling", "RE AI calling report", "Royal Enfield"),
    "re_incentive": (r"Dealer Performance Incentive Tracker", "RE dealer incentive tracker", "Royal Enfield"),
    "re_focus": (r"High Base and Low Conversion", "RE focus dealers (high base / low conversion)", "Royal Enfield"),
    "re_pen": (r"^Penetration Report as on", "RE new-vehicle penetration report", "Royal Enfield"),
    "re_history": (r"RE April to .*Renewal Performance", "RE monthly renewal history (North)", "Royal Enfield"),
    "re_visits": (r"RE_DEALER VISIT REPORT", "RE dealer visit report", "Royal Enfield"),
    "re_utr": (r"RE_UTR DETAILS", "RE payout / UTR status", "Royal Enfield"),
}


@dataclass
class SourceFile:
    key: str
    path: str
    name: str
    asof: dt.date | None
    label: str
    book: str


def discover(folder: str) -> dict[str, SourceFile]:
    """Newest file of each type (by as-of date in the name, then mtime). An all-India renewal
    report wins over a zone-only one of the same date."""
    found: dict[str, list[SourceFile]] = {}
    for p in glob.glob(os.path.join(folder, "*")):
        n = os.path.basename(p)
        if n.startswith("~$") or not n.lower().endswith((".xlsx", ".xlsb", ".xlsm")):
            continue
        for k, (rx, label, book) in FILE_TYPES.items():
            if re.search(rx, n, re.I):
                found.setdefault(k, []).append(SourceFile(k, p, n, date_from_name(n), label, book))
                break
    out = {}
    for k, lst in found.items():
        lst.sort(key=lambda f: (f.asof or dt.date.min, "north zone" not in f.name.lower(), os.path.getmtime(f.path)))
        out[k] = lst[-1]
    return out


@dataclass
class DataPack:
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    notes: list[dict] = field(default_factory=list)          # data-quality notes
    sources: dict[str, SourceFile] = field(default_factory=dict)
    asof: dict[str, str] = field(default_factory=dict)       # book -> as-of date string

    def add(self, name, df):
        self.tables[name] = df.reset_index(drop=True) if df is not None else pd.DataFrame()

    def note(self, severity, source, text):
        self.notes.append({"severity": severity, "source": source, "note": text})

    def get(self, name) -> pd.DataFrame:
        return self.tables.get(name, pd.DataFrame())


# --------------------------------------------------------------------------- #
# private car: Audi / Skoda / VW / Porsche renewal report
# --------------------------------------------------------------------------- #
SEG = {"first year": "FY", "other year": "OY"}


def _seg(label: str) -> str | None:
    t = s(label).lower()
    if "first" in t: return "FY"
    if "other" in t: return "OY"
    if "total" in t: return "TOT"
    return None


def load_pc_renewal(f: SourceFile, pack: DataPack):
    names = sheet_names(f.path)
    asof = f.asof or dt.date.today()
    cur_key = f"{asof.year:04d}-{asof.month:02d}"

    # hierarchy from the Sep'26-style snapshot sheet (RM/ZM columns labelled correctly there)
    hier = {}
    snap = next((n for n in names if re.fullmatch(r"[A-Za-z]{3}'\d{2}", n.strip())), None)
    if snap:
        g = grid(f.path, snap, max_col=20)
        h = find_row(g, lambda r: any(s(v).upper() == "DEALER CODE" for v in r))
        if h >= 0:
            hdr = [s(v).upper() for v in g[h]]
            c0 = hdr.index("DEALER CODE")
            idx = {k: hdr.index(k) for k in ("MAKE NAME", "DEALER NAME", "BRANCH", "EDME RM", "EDME ZM") if k in hdr}
            for r in g[h + 1:]:
                dc = code(r[c0])
                if not dc or dc == "GRANDTOTAL": continue
                hier[dc] = {"oem": s(r[idx["MAKE NAME"]]).upper() if "MAKE NAME" in idx else "",
                            "dealer_name": s(r[idx["DEALER NAME"]]) if "DEALER NAME" in idx else "",
                            "branch": s(r[idx["BRANCH"]]).upper() if "BRANCH" in idx else "",
                            "rm": person(r[idx["EDME RM"]]) if "EDME RM" in idx else "",
                            "zm": person(r[idx["EDME ZM"]]) if "EDME ZM" in idx else ""}

    rows = []
    swapped = 0
    for oem in ("AUDI", "SKODA", "VOLKSWAGEN", "PORSCHE"):
        if oem not in names: continue
        g = grid(f.path, oem, max_col=100)
        h = find_row(g, lambda r: s(r[0]).upper() == "DEALER CODE")
        if h < 2: pack.note("high", f.name, f"{oem}: header row not found"); continue
        p_row, s_row, m_row = ffill(g[h - 2]), ffill(g[h - 1]), g[h]
        hdr = [s(v).upper() for v in m_row]
        i_rm, i_zm = hdr.index("EDME RM") if "EDME RM" in hdr else None, hdr.index("EDME ZM") if "EDME ZM" in hdr else None
        cols = []
        closed = []
        for j in range(5, len(m_row)):
            met = s(m_row[j]).lower().rstrip(".")
            if met not in ("base", "ach", "ach.%", "ach%"): continue
            per = p_row[j]
            if isinstance(per, (dt.datetime, dt.date)):
                mk, mtd = cur_key, True
            else:
                t = s(per)
                if re.search(r"to", t, re.I): continue          # "Jan-26 To Aug-26" YTD block: recomputed from months
                mk, mtd = month_key(t), False
                if mk: closed.append(mk)
            seg = _seg(s_row[j])
            if not mk or seg not in ("FY", "OY"): continue      # totals recomputed as FY+OY
            cols.append((j, mk, mtd, seg, "base" if met == "base" else ("ach" if met == "ach" else None)))
        # Some tabs mislabel the live column with the previous month's name; the datetime header wins.
        for r in g[h + 1:]:
            dc = code(r[0])
            if not dc or "TOTAL" in dc: continue
            hi = hier.get(dc)
            if hi is None:
                # brand tabs carry RM/ZM in swapped columns (EDME RM holds the ZM) - swap back
                hi = {"oem": oem, "dealer_name": s(r[1]), "branch": s(r[2]).upper(),
                      "rm": person(r[i_zm]) if i_zm is not None else "", "zm": person(r[i_rm]) if i_rm is not None else ""}
                swapped += 1
            rec = {}
            for j, mk, mtd, seg, met in cols:
                if met is None: continue
                k = (mk, mtd, seg)
                rec.setdefault(k, {"base": 0.0, "ach": 0.0})[met] = num(r[j])
            for (mk, mtd, seg), v in rec.items():
                rows.append({"oem": oem, "dealer_code": dc, "dealer_name": hi["dealer_name"] or s(r[1]),
                             "branch": hi["branch"], "rm": hi["rm"], "zm": hi["zm"], "month": mk, "is_mtd": mtd,
                             "segment": seg, "base": v["base"], "ach": v["ach"]})
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.groupby(["oem", "dealer_code", "dealer_name", "branch", "rm", "zm", "month", "is_mtd", "segment"],
                        as_index=False)[["base", "ach"]].sum()
    pack.add("pc_dealer_month", df)
    if swapped:
        pack.note("info", f.name, f"{swapped} dealer rows not in the '{snap}' roster - RM/ZM taken from the brand tab with its "
                                  "EDME RM/EDME ZM columns swapped back (those tabs label them the wrong way round).")
    if "north zone" in f.name.lower():
        pack.note("medium", f.name, "Only the North Zone renewal report is available - national comparisons are not possible.")
    pack.asof["Private car"] = str(asof)


# --------------------------------------------------------------------------- #
# Volvo
# --------------------------------------------------------------------------- #
def load_volvo_summary(f: SourceFile, pack: DataPack):
    names = sheet_names(f.path)
    sh = next((n for n in names if "year wise" in n.lower()), None)
    rows = []
    if sh:
        g = grid(f.path, sh, max_col=100)
        h = find_row(g, lambda r: any(s(v).lower() == "base" for v in r))
        if h >= 2:
            p_row, s_row, m_row = ffill(g[h - 2]), ffill(g[h - 1]), g[h]
            cols = []
            for j, v in enumerate(m_row):
                met = s(v).lower()
                if met not in ("base", "ach"): continue
                mk = month_key(p_row[j]); seg = _seg(s_row[j])
                if mk and seg in ("FY", "OY"): cols.append((j, mk, seg, met))
            for r in g[h + 1:]:
                dc = code(r[0])
                if not dc or "TOTAL" in dc: continue
                rec = {}
                for j, mk, seg, met in cols:
                    rec.setdefault((mk, seg), {"base": 0.0, "ach": 0.0})[met] = num(r[j])
                for (mk, seg), v in rec.items():
                    rows.append({"oem": "VOLVO", "dealer_code": dc, "dealer_name": s(r[1]), "month": mk, "segment": seg, **v})
    df = pd.DataFrame(rows)
    if not df.empty:
        cur = df["month"].max()
        df["is_mtd"] = df["month"] == cur
    pack.add("volvo_dealer_month", df)
    # status label per dealer from the first sheet
    sh0 = next((n for n in names if n.lower().startswith("summary")), None)
    if sh0:
        g = grid(f.path, sh0, max_col=40)
        st = []
        for r in g[2:]:
            dc = code(r[0])
            if dc and "TOTAL" not in dc:
                last = next((v for v in reversed(r) if s(v)), None)   # status label sits in the last filled column
                st.append({"dealer_code": dc, "status": s(last) if last is not None and not isinstance(last, (int, float)) else ""})
        pack.add("volvo_status", pd.DataFrame(st))


def load_volvo_daily(f: SourceFile, pack: DataPack):
    names = sheet_names(f.path)
    # dealer roster with RM / ZM + policy-to-policy retention for the last closed and current months
    sh = next((n for n in names if n.lower().startswith("dealerwise rm")), None)
    roster, p2p = [], []
    if sh:
        g = grid(f.path, sh, max_col=30)
        h = find_row(g, lambda r: s(r[1]).lower() == "code")
        if h >= 1:
            mrow = ffill(g[h - 1])
            blocks = []
            for j, v in enumerate(g[h]):
                if s(v).lower() in ("renewal base",):
                    mk = month_key(s(mrow[j]).replace("`", ""))
                    if mk: blocks.append((j, mk))
            last_zm = ""
            for r in g[h + 1:]:
                dc = s(r[1]).upper()
                if not dc or dc.lower() == "code" or not re.match(r"^[0-9A-Z]", dc): continue
                if dc.upper().startswith("TOTAL"): continue
                roster.append({"dealer_code": dc, "dealer_name": s(r[2]), "rm": person(r[3]), "zm": person(r[4])})
                for j, mk in blocks:
                    p2p.append({"dealer_code": dc, "month": mk, "base": num(r[j]), "ach": num(r[j + 1])})
    pack.add("volvo_roster", pd.DataFrame(roster))
    pack.add("volvo_p2p_month", pd.DataFrame(p2p))

    # EV / Non-EV and year-wise split, policy level (aggregated here; no customer data kept)
    sh = next((n for n in names if n.lower().startswith("renewalretention")), None)
    if sh:
        g = grid(f.path, sh, max_col=50)
        hdr = [s(v) for v in g[0]]
        def ix(name):
            for j, v in enumerate(hdr):
                if v.lower() == name.lower(): return j
            return None
        j_exp, j_code, j_grp = ix("Policy_Expiry_Date"), ix("Code"), ix("Group Code")
        j_ren, j_yw, j_ev, j_can = ix("Renewed Program"), ix("Year wise Retention"), ix("EV / NON EV"), ix("Cancelled")
        j_ins, j_model = ix("Insurance_Company"), ix("Model")
        agg = {}
        for r in g[1:]:
            if j_exp is None or not r[j_exp]: continue
            if j_can is not None and s(r[j_can]).lower() == "yes": continue
            mk = month_key(r[j_exp])
            k = (s(r[j_grp] if j_grp is not None else r[j_code]).upper(), mk,
                 "FY" if "1st" in s(r[j_yw]).lower() else "OY", s(r[j_ev]) if j_ev is not None else "")
            a = agg.setdefault(k, [0, 0]); a[0] += 1
            if s(r[j_ren]).lower() == "renewed": a[1] += 1
        df = pd.DataFrame([{"dealer_code": k[0], "month": k[1], "segment": k[2], "ev": k[3] if k[3] not in ("#N/A", "") else "Unknown",
                            "base": v[0], "ach": v[1]} for k, v in agg.items()])
        pack.add("volvo_expiry_month", df)


def load_volvo_conversion(f: SourceFile, pack: DataPack):
    names = sheet_names(f.path)
    sh = next((n for n in names if "not renewed" in n.lower()), None)
    if not sh: return
    g = grid(f.path, sh, max_col=70)
    hdr = [s(v) for v in g[0]]
    keep = {"Dealer Code": "dealer_code", "Dealer Name": "dealer_name", "Previous policy type": "prev_policy_type",
            "Cover Type": "cover_type", "Insurance Company Name": "insurer", "Policy End Date": "expiry_date",
            "Model": "model", "Is Add on": "has_addon", "NET OD PREM": "net_od_premium", "TOTAL PREM": "total_premium",
            "TOTAL CLAIMS": "claims", "MFG YEAR": "mfg_year", "City Name": "city", "Year": "segment_label",
            "Year 2": "policy_year", "RM": "rm", "ZM": "zm"}
    idx = {}
    for j, v in enumerate(hdr):
        if v in keep and keep[v] not in idx.values():
            # duplicate "Dealer Code" header: the second one is the group code - keep that (joins to retention sheets)
            if v == "Dealer Code" and "dealer_code" in idx.values(): continue
            idx[j] = keep[v]
    dup = [j for j, v in enumerate(hdr) if v == "Dealer Code"]
    if len(dup) > 1:
        idx = {k: v for k, v in idx.items() if v != "dealer_code"}; idx[dup[1]] = "dealer_code"
    recs = []
    for r in g[1:]:
        if not any(r): continue
        d = {n: r[j] for j, n in idx.items()}
        d["dealer_code"] = s(d.get("dealer_code")).upper()
        for c in ("net_od_premium", "total_premium", "claims", "mfg_year"): d[c] = num(d.get(c))
        d["expiry_date"] = pd.to_datetime(d.get("expiry_date"), errors="coerce")
        d["segment"] = "FY" if "first" in s(d.get("segment_label")).lower() else "OY"
        d["rm"], d["zm"] = person(d.get("rm")), person(d.get("zm"))
        recs.append(d)
    df = pd.DataFrame(recs).drop(columns=["segment_label"], errors="ignore")
    pack.add("volvo_open_cases", df)
    pack.note("info", f.name, f"{len(df)} Volvo policies expired this month and are not renewed; customer identifiers "
                              "(name, policy, chassis, registration, contact, address) are deliberately not loaded.")


# --------------------------------------------------------------------------- #
# SAVWIPL penetration (new-car policies vs retail) + monthly renewal from the same MIS
# --------------------------------------------------------------------------- #
MONTH_RX = re.compile(r"^(jan|feb|mar|march|apr|april|may|jun|june|jul|july|aug|sep|oct|nov|dec)[a-z]*'(\d{2})\b", re.I)


def load_savwipl_pen(f: SourceFile, pack: DataPack):
    names = sheet_names(f.path)
    cands = [n for n in names if re.match(r"MIS .*\d{2}\s*to\s*.*\d{2}", n, re.I)]
    if not cands: pack.note("medium", f.name, "No 'MIS <from> to <to>' sheet found"); return
    def end_key(n):
        ks = [month_key(x) for x in re.findall(r"[A-Za-z]+'\d{2}", n)]
        return max([k for k in ks if k] or ["0000-00"])
    sh = max(cands, key=end_key)
    g = grid(f.path, sh, max_col=420)
    h = find_row(g, lambda r: s(r[0]).upper() == "OEM" and any(s(v).upper() == "MISP DEALER CODE" for v in r))
    if h < 0: pack.note("medium", f.name, f"{sh}: header not found"); return
    hdr = [s(v) for v in g[h]]
    fix = {}
    for name in ("OEM", "MISP DEALER CODE", "Investor", "State", "CITY", "ABIBL BRANCH", "ABIBL RM NEW", "ABIBL ZM Name",
                 "MISP STATUS", "Region", "Group name"):
        for j, v in enumerate(hdr):
            if v.lower() == name.lower(): fix[name] = j; break
    # month blocks: named columns carry the month prefix; unnamed ones (Penetration, Retention %) inherit it
    colmap = []
    cur = None
    for j, v in enumerate(hdr):
        m = MONTH_RX.match(v)
        if m: cur = month_key(f"{m.group(1)[:3]}'{m.group(2)}")
        if not cur: continue
        t = v.lower()
        if t.endswith(" retail") and "target" not in t: colmap.append((j, cur, "retail"))
        elif "total new policies" in t: colmap.append((j, cur, "new_nop"))
        elif "total new od premium" in t: colmap.append((j, cur, "new_od_premium"))
        elif "renewal base" in t: colmap.append((j, cur, "renewal_base"))
        elif "total renewal policies" in t: colmap.append((j, cur, "renewed"))
        elif "rollover case" in t: colmap.append((j, cur, "rollover"))
    rows = []
    for r in g[h + 1:]:
        dc = s(r[fix.get("MISP DEALER CODE", 5)]).upper()
        oem = s(r[fix.get("OEM", 0)]).upper()
        if not dc or not oem or oem in ("GRAND TOTAL",) or "TOTAL" in dc: continue
        base = {"oem": oem, "dealer_code": dc, "dealer_name": s(r[fix["Investor"]]) if "Investor" in fix else "",
                "state": s(r[fix["State"]]).upper() if "State" in fix else "", "city": s(r[fix["CITY"]]).upper() if "CITY" in fix else "",
                "branch": s(r[fix["ABIBL BRANCH"]]).upper() if "ABIBL BRANCH" in fix else "",
                "rm": person(r[fix["ABIBL RM NEW"]]) if "ABIBL RM NEW" in fix else "",
                "zm": person(r[fix["ABIBL ZM Name"]]) if "ABIBL ZM Name" in fix else "",
                "misp_status": s(r[fix["MISP STATUS"]]) if "MISP STATUS" in fix else ""}
        per = {}
        for j, mk, met in colmap:
            per.setdefault(mk, {})[met] = num(r[j])
        for mk, v in per.items():
            rows.append({**base, "month": mk, **{k: v.get(k, 0.0) for k in
                         ("retail", "new_nop", "new_od_premium", "renewal_base", "renewed", "rollover")}})
    df = pd.DataFrame(rows)
    pack.add("pc_penetration_month", df)
    pack.note("info", f.name, f"Penetration read from sheet '{sh}' (the other {len(names) - 1} sheets are 2023-25 history, "
                              "pivots and monthly retail extracts).")


# --------------------------------------------------------------------------- #
# payouts (UTR) - SAVWIPL/Volvo and RE share one layout
# --------------------------------------------------------------------------- #
def load_utr(f: SourceFile, pack: DataPack, table: str):
    names = sheet_names(f.path)
    if "DATA" not in names: pack.note("medium", f.name, "DATA sheet missing"); return
    rows = iter_sheet(f.path, "DATA", max_col=70)   # ~60k rows: stream, never hold the whole sheet
    hdr = [s(v) for v in next(rows, [])]
    want = {"Dealer Code": "dealer_code", "FY": "fy", "OEM": "oem", "Sub OEM Name": "sub_oem", "ABIBL BRANCH_NAME": "branch",
            "Base Value": "base_value", "Final NET AMT": "net_amount", "Invoice Stage": "invoice_stage",
            "Payment Date": "payment_date", "Final Status": "status", "Edme RM Name": "rm", "Edme KPH Name": "zm",
            "MONTH_YEAR": "month_year", "MISP Name": "dealer_name", "Invoice Type": "invoice_type"}
    idx = {}
    for j, v in enumerate(hdr):
        if v in want and want[v] not in idx.values(): idx[j] = want[v]
    recs = []
    for r in rows:
        if not r or r[0] is None: continue
        d = {n: (r[j] if j < len(r) else None) for j, n in idx.items()}
        d["dealer_code"] = re.sub(r"_.*$", "", code(d.get("dealer_code")))
        d["base_value"], d["net_amount"] = num(d.get("base_value")), num(d.get("net_amount"))
        d["payment_date"] = excel_date(d.get("payment_date"))
        my = excel_date(d.get("month_year"))
        d["month"] = f"{my.year:04d}-{my.month:02d}" if pd.notna(my) else None
        d.pop("month_year", None)
        d["rm"], d["zm"] = person(d.get("rm")), person(d.get("zm"))
        d["status"] = s(d.get("status"))
        d["paid"] = d["status"].lower() == "already paid"
        so = s(d.get("sub_oem")).upper()
        d["oem"] = ("ROYAL ENFIELD" if "ROYAL" in so else "VOLVO" if "VOLVO" in so else so or s(d.get("oem")).upper())
        recs.append(d)
    df = pd.DataFrame(recs).drop(columns=["sub_oem"], errors="ignore")
    pack.add(table, df)


# --------------------------------------------------------------------------- #
# Royal Enfield
# --------------------------------------------------------------------------- #
def load_re_renewal(f: SourceFile, pack: DataPack):
    names = sheet_names(f.path)
    asof = f.asof or dt.date.today()
    # region FY / OY with AI reach
    sh = next((n for n in names if n.lower().startswith("year wise")), None)
    if sh:
        g = grid(f.path, sh, max_col=26)
        h = find_row(g, lambda r: any(s(v).lower() == "invite base" for v in r))
        rows, zone = [], ""
        for r in g[h + 1:]:
            z = s(r[4])
            if z and "total" not in z.lower(): zone = z.upper()
            reg = s(r[5]).upper()
            if not re.fullmatch(r"[NCEWS]\d", reg): continue
            rows.append({"zone": zone, "region": reg, "state": s(r[6]), "dealers": num(r[7]), "ai_dealers": num(r[8]),
                         "fy_base_prev": num(r[9]), "fy_ach_prev": num(r[10]), "fy_base": num(r[12]), "fy_ach": num(r[15]),
                         "oy_base": num(r[17]), "oy_ach": num(r[18]), "tot_base": num(r[22]), "tot_ach": num(r[23])})
        pack.add("re_region", pd.DataFrame(rows))
    # region MTD vs same day last month + daily renewals
    sh = next((n for n in names if "vs" in n.lower() and "summary" in n.lower()), None)
    if sh:
        g = grid(f.path, sh, max_col=50)
        h = find_row(g, lambda r: any(s(v).lower() == "mtd target" for v in r))
        dayrow = g[h]
        mrow = ffill(g[h - 1]) if h >= 1 else [None] * len(dayrow)
        dcols = [(j, int(num(v))) for j, v in enumerate(dayrow) if isinstance(v, (int, float)) and 1 <= num(v) <= 31]
        mstart = next((mrow[j] for j, _ in dcols if isinstance(mrow[j], (dt.datetime, dt.date))), None)
        regs, daily = [], []
        for r in g[h + 1:]:
            reg = s(r[1]).upper()
            if not re.fullmatch(r"[NCEWS]\d", reg): continue
            regs.append({"region": reg, "state": s(r[2]), "mtd_target_prev": num(r[3]), "mtd_actual_prev": num(r[4]),
                         "mtd_target": num(r[6]), "mtd_actual": num(r[7]), "growth_nop": num(r[9])})
            for j, d in dcols:
                if mstart is not None:
                    try: day = dt.date(mstart.year, mstart.month, d)
                    except ValueError: continue
                    if day > asof: continue
                    daily.append({"region": reg, "date": pd.Timestamp(day), "renewals": num(r[j])})
        pack.add("re_region_mtd", pd.DataFrame(regs))
        pack.add("re_daily", pd.DataFrame(daily))
    # dealer MTD comparison
    sh = next((n for n in names if "dealer_wise" in n.lower() or "dealer wise comparison" in n.lower()), None)
    if sh:
        g = grid(f.path, sh, max_col=16)
        rows = []
        for r in g[2:]:
            dc = code(r[0])
            if not dc or not re.match(r"^\d", dc): continue
            rows.append({"dealer_code": dc, "dealer_name": s(r[1]), "region": s(r[4]).upper(),
                         "mtd_target_prev": num(r[5]), "mtd_actual_prev": num(r[6]),
                         "mtd_target": num(r[8]), "mtd_actual": num(r[9]), "activation": s(r[12])})
        pack.add("re_dealer_mtd", pd.DataFrame(rows))
    # EDME 360 calling
    sh = next((n for n in names if "edme 360" in n.lower()), None)
    if sh:
        g = grid(f.path, sh, max_col=40)
        h = find_row(g, lambda r: any(s(v).upper() == "DEALER CODE" for v in r))
        hdr = [s(v) for v in g[h]]
        j_mtd = next((j for j, v in enumerate(hdr) if v.lower() == "calling nop mtd"), None)
        dcols = [(j, re.search(r"(\d{2}-\d{2}-\d{4})", v).group(1)) for j, v in enumerate(hdr) if re.search(r"\d{2}-\d{2}-\d{4}", v)]
        rows = []
        for r in g[h + 1:]:
            dc = code(r[3])
            if not dc: continue
            rows.append({"dealer_code": dc, "dealer_name": s(r[4]), "rm": person(r[5]), "zm": person(r[6]), "region": s(r[7]).upper(),
                         "base": num(r[8]), "calls_mtd": num(r[j_mtd]) if j_mtd is not None else sum(num(r[j]) for j, _ in dcols),
                         "active_days": sum(1 for j, _ in dcols if num(r[j]) > 0)})
        pack.add("re_edme", pd.DataFrame(rows))
    pack.asof["Royal Enfield"] = str(asof)


def load_re_ai(f: SourceFile, pack: DataPack):
    names = sheet_names(f.path)
    sh = next((n for n in names if n.lower().startswith("dealer wise")), None)
    if sh:
        g = grid(f.path, sh, max_col=12)
        rows = []
        for r in g[2:]:
            dc = code(r[0])
            if not dc or "TOTAL" in dc: continue
            rows.append({"dealer_code": dc, "dealer_name": s(r[1]), "region": s(r[2]).upper(), "fy_base": num(r[3]),
                         "calls": num(r[4]), "failed": num(r[5]), "disposed": num(r[6]), "abandoned": num(r[7]),
                         "conversions": num(r[8])})
        pack.add("re_ai_dealer", pd.DataFrame(rows))
    sh = next((n for n in names if "daily" in n.lower()), None)
    if sh:
        g = grid(f.path, sh, max_col=14)
        rows = []
        for r in g:
            d = r[2]
            if not isinstance(d, (dt.datetime, dt.date)): continue
            rows.append({"date": pd.Timestamp(d), "calls_prev": num(r[3]), "disposed_prev": num(r[4]), "conv_prev": num(r[5]),
                         "calls": num(r[7]), "failed": num(r[8]), "disposed": num(r[9]), "abandoned": num(r[10]),
                         "conversions": num(r[11])})
        pack.add("re_ai_daily", pd.DataFrame(rows))
    sh = next((n for n in names if n.lower().startswith("region")), None)
    if sh:
        g = grid(f.path, sh, max_col=22)
        rows, zone = [], ""
        for r in g:
            z = s(r[3])
            if z and "total" not in z.lower() and "region" not in z.lower(): zone = z.upper()
            reg = s(r[4]).upper()
            if not re.fullmatch(r"[NCEWS]\d", reg): continue
            rows.append({"zone": zone, "region": reg, "state": s(r[5]), "fy_base": num(r[6]), "dealers_with_base": num(r[7]),
                         "consent_dealers": num(r[8]), "ai_dealers": num(r[9]), "ai_base": num(r[10]),
                         "calls_prev": num(r[11]), "disposed_prev": num(r[12]), "conv_prev": num(r[13]),
                         "calls": num(r[15]), "failed": num(r[16]), "disposed": num(r[17]), "abandoned": num(r[18]),
                         "conversions": num(r[19])})
        df = pd.DataFrame(rows)
        if not df.empty and (df["consent_dealers"] > df["dealers_with_base"]).any():
            pack.note("low", f.name, "AI 'Consent Given' dealer counts exceed 'Total Dealer In 1st Year Base' in "
                                     f"{int((df['consent_dealers'] > df['dealers_with_base']).sum())} regions (counts sub-codes vs dealers).")
        pack.add("re_ai_region", df)


def load_re_incentive(f: SourceFile, pack: DataPack):
    g = grid(f.path, "Master Data", max_col=48) if "Master Data" in sheet_names(f.path) else []
    rows = []
    for r in g[2:]:
        dc = code(r[0])
        if not dc or not re.match(r"^\d", dc): continue
        tiers = [(5, r[42], r[26]), (10, r[43], r[30]), (15, r[44], r[34]), (20, r[45], r[38])]
        q = [t for t, flag, _ in tiers if s(flag).lower() == "yes"]
        earned = next((num(a) for t, flag, a in reversed(tiers) if s(flag).lower() == "yes"), 0.0)
        ach, base = num(r[39]), num(r[22])
        nxt = next((t for t in (5, 10, 15, 20) if t not in q), None)
        tgt_pct = {5: num(r[23]), 10: num(r[27]), 15: num(r[31]), 20: num(r[35])}
        need = max(0.0, tgt_pct[nxt] * base - ach) if nxt else 0.0
        rows.append({"dealer_code": dc, "dealer_name": s(r[1]), "dealer_group": s(r[2]), "region": s(r[4]).upper(),
                     "state": s(r[5]).upper(), "city": s(r[6]).upper(), "rm": person(r[7]), "zone": s(r[8]).upper(),
                     "jul_base": num(r[11]), "jul_ach": num(r[15]), "retention_band": s(r[21]),
                     "fy_base": base, "fy_ach": ach, "tier": max(q) if q else 0, "incentive_earned": earned,
                     "next_tier": nxt, "nop_to_next_tier": round(need, 1),
                     "target_5": tgt_pct[5], "target_20": tgt_pct[20]})
    pack.add("re_incentive", pd.DataFrame(rows))


def load_re_focus(f: SourceFile, pack: DataPack):
    names = sheet_names(f.path)
    if "Data" not in names: return
    g = grid(f.path, "Data", max_col=26)
    h = find_row(g, lambda r: s(r[0]).lower() == "dealer code")
    hdr = [s(v) for v in g[h]]
    rows = []
    for r in g[h + 1:]:
        dc = code(r[0])
        if not dc: continue
        acts = [s(v) for v in r[14:26]]
        rows.append({"dealer_code": dc, "ai_consent": s(r[1]).title(), "dealer_group": s(r[2]), "rm": person(r[3]),
                     "zm": person(r[4]), "zone": s(r[5]).upper(), "region": s(r[6]).upper(),
                     "base_prev": num(r[7]), "ach_prev": num(r[8]), "base": num(r[10]), "ach": num(r[11]),
                     "actions_logged": sum(1 for a in acts if a)})
    df = pd.DataFrame(rows)
    if not df.empty and df["actions_logged"].sum() == 0:
        pack.note("high", f.name, f"Action-tracking columns (visit date, min NOP/day, weekly target, support required) are "
                                  f"empty for all {len(df)} focus dealers.")
    pack.add("re_focus", df)


def load_re_pen(f: SourceFile, pack: DataPack):
    g = grid(f.path, "Main Sheet", max_col=46) if "Main Sheet" in sheet_names(f.path) else []
    rows = []
    for r in g[2:]:
        dc = code(r[0])
        if not dc or not re.match(r"^\d", dc): continue
        rows.append({"dealer_code": dc, "dealer_name": s(r[1]), "dealer_group": s(r[2]), "region": s(r[3]).upper(),
                     "zone": s(r[4]).upper(), "branch": s(r[5]).upper(), "rm": person(r[6]), "state": s(r[8]).upper(),
                     "veh_prev": num(r[9]), "veh_mtd": num(r[10]), "veh_ytd": num(r[11]),
                     "prog_prev": num(r[15]), "prog_mtd": num(r[16]), "prog_ytd": num(r[17]),
                     "nop_mtd": num(r[19]), "nop_ytd": num(r[20]), "addon_mtd": num(r[22]), "addon_ytd": num(r[23]),
                     "essential": num(r[31]), "advance": num(r[32]), "premium": num(r[33]), "bundled": num(r[34]),
                     "aloa": num(r[35]), "no_addon": num(r[37])})
    df = pd.DataFrame(rows)
    n0 = len(df)
    df = drop_total_rows(df, ["veh_mtd", "prog_mtd"])
    if len(df) < n0:
        pack.note("medium", f.name, "Penetration 'Main Sheet' contains a hidden grand-total row - removed before aggregating.")
    pack.add("re_penetration", df)


def load_re_history(f: SourceFile, pack: DataPack):
    rows = []
    for sh in sheet_names(f.path):
        mk = month_key(sh.replace("-", "'"))
        if not mk: continue
        g = grid(f.path, sh, max_col=25)
        h = find_row(g, lambda r: s(r[0]).upper() == "DEALER CODE" or s(r[0]).upper().endswith("DEALER CODE"))
        if h < 0:
            h = find_row(g, lambda r: any(s(v).upper() == "DEALER CODE" for v in r))
        if h < 0: continue
        hdr = [s(v).upper() for v in g[h]]
        i_rm = hdr.index("EIBL RM") if "EIBL RM" in hdr else None
        last9 = len([v for v in hdr if v]) - 9
        for r in g[h + 1:]:
            dc = code(r[0])
            if not dc or not re.match(r"^\d", dc): continue
            vals = [num(v) for v in r[last9:last9 + 9]]
            rows.append({"dealer_code": dc, "dealer_name": s(r[1]), "region": s(r[2]).upper(), "month": mk,
                         "rm": person(r[i_rm]) if i_rm is not None else "",
                         "fy_base": vals[0], "fy_ach": vals[1], "oy_base": vals[3], "oy_ach": vals[4]})
    df = pd.DataFrame(rows)
    if not df.empty:
        cur = df["month"].max(); df["is_mtd"] = df["month"] == cur
    pack.add("re_history", df)
    pack.note("info", f.name, "Monthly RE history covers the North zone only (regions N1-N5, C1-C3); the latest month is month-to-date.")


def load_re_visits(f: SourceFile, pack: DataPack):
    names = sheet_names(f.path)
    rows = []
    for sh in ("DEALER VISIT REPORT", "Other Visits"):
        if sh not in names: continue
        g = grid(f.path, sh, max_col=51)
        for r in g[1:]:
            d = r[7]
            if not isinstance(d, (dt.datetime, dt.date)): continue
            params = set()
            for j in (19, 24, 31, 35, 39):
                for p in s(r[j]).split(";"):
                    if p.strip(): params.add(p.strip())
            rows.append({"date": pd.Timestamp(d), "rm": person(r[8]), "visit_type": s(r[9]), "dealer_code": code(r[10]),
                         "dealer_name": s(r[11]), "joint_visit": s(r[12]), "region": s(r[13]).upper(), "zone": s(r[14]).upper(),
                         "state": s(r[15]).upper(), "met_dp_gm": s(r[42]).lower() == "yes", "met_insurance": s(r[43]).lower() == "yes",
                         "met_workshop": s(r[44]).lower() == "yes", "telecaller": s(r[25]).title(),
                         "telecaller_experience": s(r[26]), "topics": "; ".join(sorted(params)),
                         "support_requested": s(r[27])[:300]})
    df = pd.DataFrame(rows)
    pack.add("re_visits", df)
    if not df.empty:
        bad = (df["date"] < pd.Timestamp("2026-06-01")).sum()
        if bad: pack.note("low", f.name, f"{bad} visits dated before the form went live (1-Jun-2026) - probably a mistyped month.")
    if "Dealer Master" in names:
        g = grid(f.path, "Dealer Master", max_col=13)
        m = []
        for r in g[1:]:
            dc = code(r[0])
            if not dc: continue
            m.append({"dealer_code": dc, "dealer_name": s(r[1]), "city": s(r[2]).upper(), "state": s(r[3]).upper(),
                      "rm": person(r[4]), "zm": person(r[5]), "branch": s(r[6]).upper(), "group_code": code(r[7]),
                      "retail_mtd": num(r[8]), "retail_ytd": num(r[12])})
        pack.add("re_dealer_master", pd.DataFrame(m))


LOADERS = {
    "pc_renewal": load_pc_renewal, "volvo_summary": load_volvo_summary, "volvo_daily": load_volvo_daily,
    "volvo_conversion": load_volvo_conversion, "savwipl_pen": load_savwipl_pen,
    "savwipl_utr": lambda f, p: load_utr(f, p, "pc_payouts"), "re_utr": lambda f, p: load_utr(f, p, "re_payouts"),
    "re_renewal": load_re_renewal, "re_ai": load_re_ai, "re_incentive": load_re_incentive, "re_focus": load_re_focus,
    "re_pen": load_re_pen, "re_history": load_re_history, "re_visits": load_re_visits,
}


def load_folder(folder: str, progress=None) -> DataPack:
    pack = DataPack()
    pack.sources = discover(folder)
    for k, (rx, label, book) in FILE_TYPES.items():
        if k not in pack.sources:
            pack.note("medium" if k in ("pc_renewal", "re_renewal") else "low", label, "File not found in this drop.")
    for i, (k, f) in enumerate(pack.sources.items()):
        if progress: progress(i / max(1, len(pack.sources)), f.name)
        try:
            LOADERS[k](f, pack)
        except Exception as e:  # one bad file must not stop the rest
            pack.note("high", f.name, f"Could not be read: {type(e).__name__}: {e}")
    from .model import finalize
    finalize(pack)
    return pack
