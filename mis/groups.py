"""Dealer groups (clubbed dealers) for the private-car brands.

A group is one dealer principal trading under several codes - main codes, sub-outlets (penetration file codes like
SK1010001C belong to SK10100) and sister companies (Elysian + Wheelocity). Groups are clubbed within one brand: an Audi
code and a Skoda code of the same family stay in their own brand books.

Every grouped table shows the codes line by line first, then the group total (sum of base / renewed, retail / policies,
never an average of percentages).
"""
from __future__ import annotations

import re

import pandas as pd

# (group name, regex on the dealer name) - user-confirmed list (30-Sep-2026) plus northern-state names that repeat
GROUP_RULES = [
    ("Sidak", r"\bSIDAK\b"), ("Berk", r"\bBERK\b"), ("Regent", r"\bREGENT\b"), ("Kanish", r"\bKANISH\b"),
    ("PSB", r"\bPSB\b"), ("BLC", r"\bBLC\b"), ("Speedworks", r"\bSPEEDWORKS?\b"),
    ("Elysian + Wheelocity", r"\bELYSIAN\b|\bWHEELOCITY\b"), ("Malwa", r"\bMALWA\b"), ("Liftech", r"\bLIFTECH\b"),
    ("MN", r"(^|M/S\s*)M\.?\s?N\.?\s"), ("Maa Sheetla", r"\bSHEETLA\b"), ("PAL", r"\bPAL\b"),
    # found in the northern states (UP, Delhi, Chandigarh, Punjab, J&K, Haryana, Uttarakhand, HP) with several codes
    ("Lally", r"\bLALLY\b"), ("United Wheels", r"\bUNITED WHEELS\b"), ("Viraj", r"\bVIRAJ\b"),
]
USER_GROUPS = {g for g, _ in GROUP_RULES[:13]}
CAR_OEMS = {"AUDI", "SKODA", "VOLKSWAGEN", "PORSCHE", "VOLVO"}
_OUTLET = re.compile(r"^([A-Z]{2}\d{5})\d{2}[A-Z]$")      # SK1010001C -> SK10100


def group_of(name: str, oem: str = "") -> str | None:
    if oem and oem.upper() not in CAR_OEMS: return None
    n = str(name or "").upper()
    for g, rx in GROUP_RULES:
        if re.search(rx, n): return g
    return None


def parent_code(code: str) -> str:
    m = _OUTLET.match(str(code or "").upper())
    return m.group(1) if m else str(code or "")


def tag(df: pd.DataFrame, name_col="dealer_name", oem_col="oem") -> pd.DataFrame:
    """Add a 'group' column (None when not clubbed)."""
    d = df.copy()
    oems = d[oem_col] if oem_col in d else pd.Series("", index=d.index)
    d["group"] = [group_of(n, o) for n, o in zip(d[name_col], oems)]
    return d


def club(df: pd.DataFrame, key: str, sums: list[str], ratios: dict, label_col: str, name_col: str | None = None,
         sort_col: str | None = None, ascending=True) -> pd.DataFrame:
    """Line items then group total. `df` has one row per code with a 'group' column; `ratios` = {out: (num, den)}.
    Ungrouped codes stay as single rows. Returns rows in display order with a 'Level' column (Group / ↳ code / Dealer)."""
    if df.empty or "group" not in df:
        return df
    out = []
    singles = df[df["group"].isna()].copy()
    singles["Level"] = "Dealer"
    grouped = df[df["group"].notna()]
    blocks = []
    for g, x in grouped.groupby("group"):
        if len(x) == 1:
            r = x.copy(); r["Level"] = "Dealer"; r[label_col] = r[label_col].astype(str) + f"  [{g}]"
            singles = pd.concat([singles, r]); continue
        items = x.copy(); items["Level"] = "↳ code"
        items[label_col] = "   ↳ " + items[label_col].astype(str)
        tot = {c: x[c].sum() for c in sums}
        for o, (nu, de) in ratios.items():
            tot[o] = (tot[nu] / tot[de]) if tot.get(de) else float("nan")
        tot.update({label_col: f"Σ {g} group ({len(x)} codes)", key: f"{g} (group)", "Level": "Group", "group": g})
        if sort_col and sort_col not in tot and sort_col in x and sort_col != "group":
            tot[sort_col] = pd.to_numeric(x[sort_col], errors="coerce").min() if ascending else pd.to_numeric(x[sort_col], errors="coerce").max()
        for c in x.columns:
            if c not in tot:
                v = x[c].dropna().unique()
                tot[c] = v[0] if len(v) == 1 else ""
        blocks.append((tot.get(sort_col) if sort_col and sort_col != "group" else g, pd.concat([items, pd.DataFrame([tot])], ignore_index=True)))
    if sort_col and sort_col in singles:
        singles = singles.sort_values(sort_col, ascending=ascending)
    rows = [singles.assign(_o=singles[sort_col] if sort_col and sort_col in singles else 0)]
    for k, b in blocks:
        rows.append(b.assign(_o=k if k == k else 0))
    allr = pd.concat(rows, ignore_index=True)
    allr["_o"] = pd.to_numeric(allr["_o"], errors="coerce").fillna(float("inf") if ascending else float("-inf")) \
        if sort_col and sort_col != "group" else allr["_o"].astype(str)
    # keep each group block together: order blocks by their total, items stay above their total
    allr["_blk"] = allr["group"].fillna(allr[key].astype(str))
    order = allr[allr["Level"].isin(["Dealer", "Group"])].sort_values("_o", ascending=ascending)["_blk"].tolist()
    rank = {b: i for i, b in enumerate(dict.fromkeys(order))}
    allr["_r"] = allr["_blk"].map(rank)
    allr["_l"] = allr["Level"].map({"↳ code": 0, "Dealer": 1, "Group": 2})
    return allr.sort_values(["_r", "_l"]).drop(columns=["_o", "_blk", "_r", "_l"]).reset_index(drop=True)
