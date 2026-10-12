"""Post-load modelling: one unified retention fact table, joins, targets, data-quality checks and the
dataset catalogue the agent uses."""
from __future__ import annotations

import pandas as pd

from .util import person

# Targets (fractions) - defaults, editable in the app's Settings (or TARGETS_JSON secret).
# Evidence in the MIS: RE first-year "Target% 30%" (RESP renewal report); Volvo "75% Target from Base".
# Audi/Skoda/VW carry no target in the files - 65/75/70 is a placeholder until set.
TARGETS = {
    "AUDI": {"FY": .65, "OY": .75, "TOT": .70},
    "SKODA": {"FY": .65, "OY": .75, "TOT": .70},
    "VOLKSWAGEN": {"FY": .65, "OY": .75, "TOT": .70},
    "PORSCHE": {"FY": .65, "OY": .75, "TOT": .70},
    "VOLVO": {"FY": .65, "OY": .75, "TOT": .75},
    "ROYAL ENFIELD": {"FY": .45, "OY": .75, "TOT": .50},   # RESP programme targets (the MIS "30%" column is an interim milestone)
}
OEMS = ["ROYAL ENFIELD", "AUDI", "SKODA", "VOLKSWAGEN", "VOLVO", "PORSCHE"]
OEM_LABEL = {"ROYAL ENFIELD": "Royal Enfield", "AUDI": "Audi", "SKODA": "Skoda", "VOLKSWAGEN": "Volkswagen",
             "VOLVO": "Volvo", "PORSCHE": "Porsche"}
RE_REGION_ZM = {"N": "NAMAN SINGH", "C": "NAMAN SINGH", "E": "MANORANJAN SARANGI", "S": "EZHILARASAN P", "W": "KALPESH TANK"}


def re_zm(region: str) -> str:
    region = (region or "").upper()
    if region == "C4": return "MANORANJAN SARANGI"
    return RE_REGION_ZM.get(region[:1], "")


def finalize(pack):
    t = pack.tables

    # ---------- unified retention fact: book / oem / dealer / hierarchy / month / segment ----------
    parts = []
    pc = t.get("pc_dealer_month", pd.DataFrame())
    if not pc.empty:
        parts.append(pc.assign(book="Private car", region=""))
    vd = t.get("volvo_dealer_month", pd.DataFrame())
    ve = t.get("volvo_expiry_month", pd.DataFrame())
    if vd.empty and not ve.empty:
        # No summary file in this drop: rebuild FY/OY by month from the policy-level expiry cohort (it reconciles
        # to the summary's totals). Only months up to the as-of month count; later expiries are early renewals.
        asof = max([str(f.asof) for k, f in pack.sources.items() if k.startswith("volvo") and f.asof] or ["9999-12"])[:7]
        x = ve[ve["month"] <= asof].groupby(["dealer_code", "month", "segment"], as_index=False)[["base", "ach"]].sum()
        ros = t.get("volvo_roster", pd.DataFrame())
        names = {}
        if not ros.empty:
            for dc, nm in ros[["dealer_code", "dealer_name"]].itertuples(index=False):
                for part in dc.split("/"): names.setdefault(part, nm); names.setdefault(dc, nm)
        x["dealer_name"] = x["dealer_code"].map(names).fillna("")
        x["oem"] = "VOLVO"; x["is_mtd"] = x["month"] == asof
        vd = x
        pack.add("volvo_dealer_month", vd)
        pack.note("info", "Volvo", "Volvo FY/OY by month rebuilt from the daily workbook's policy-level expiry sheet "
                                   "(Apr-26 onward); add the 'VOLVO Retention Summary' file for Jan-Mar.")
    if not vd.empty:
        ros = t.get("volvo_roster", pd.DataFrame())
        # Volvo roster ZM column is sparse; fall back to the ZM of the same RM elsewhere, then the open-cases file
        rm_zm = {}
        for src in ("pc_dealer_month", "volvo_open_cases"):
            d = t.get(src, pd.DataFrame())
            if not d.empty and "rm" in d and "zm" in d:
                for rm, zm in d[["rm", "zm"]].dropna().drop_duplicates().itertuples(index=False):
                    if rm and zm: rm_zm.setdefault(rm, zm)
        # The roster's ZM column is misaligned in the daily workbook (e.g. Ludhiana / Faridabad / Gurgaon dealers shown
        # under a West ZM). The open-cases and payout files carry a per-dealer ZM that agree with each other - use them.
        dz = {}
        for src in ("volvo_open_cases", "pc_payouts"):
            d = t.get(src, pd.DataFrame())
            if d.empty or "zm" not in d: continue
            if "oem" in d: d = d[d["oem"] == "VOLVO"]
            for code, zm in d[["dealer_code", "zm"]].itertuples(index=False):
                zm = str(zm or "").strip()
                if not zm or zm.upper() == "DIRECT CODE": continue
                for part in str(code).upper().split("/"):
                    dz.setdefault(part.strip(), {}).setdefault(zm, 0)
                    dz[part.strip()][zm] += 1
        def dealer_zm(code):
            votes = {}
            for part in str(code).upper().split("/"):
                for zm, n in dz.get(part.strip(), {}).items(): votes[zm] = votes.get(zm, 0) + n
            return max(votes, key=votes.get) if votes else ""
        if not ros.empty:
            ros = ros.copy()
            fixed = 0
            new = []
            for c, r, z in zip(ros["dealer_code"], ros["rm"], ros["zm"]):
                z2 = dealer_zm(c) or z or rm_zm.get(r, "")
                fixed += bool(z and z2 and z2 != z)
                new.append(z2)
            ros["zm"] = new
            if fixed:
                pack.note("medium", "Volvo", f"{fixed} Volvo dealers had a ZM in the daily workbook's roster that contradicts the "
                                             "open-cases and payout files; the dealer-level ZM from those files is used.")
            vd = vd.merge(ros[["dealer_code", "rm", "zm"]], on="dealer_code", how="left")
        else:
            vd = vd.assign(rm="", zm="")
        parts.append(vd.assign(book="Private car", branch="", region="").fillna({"rm": "", "zm": ""}))
    rh = t.get("re_history", pd.DataFrame())
    dmail = t.get("re_dailymail", pd.DataFrame())
    if not dmail.empty:
        # national dealer-wise month (daily mail) - replaces the North-only history for the same month
        if not rh.empty:
            rh = rh[~rh["month"].isin(set(dmail["month"]))].copy()
            rh["is_mtd"] = False
        keep = ["dealer_code", "dealer_name", "region", "month", "rm", "zm", "fy_base", "fy_ach", "oy_base", "oy_ach", "is_mtd"]
        rh = pd.concat([rh, dmail[keep]], ignore_index=True)
    if not rh.empty:
        dm = t.get("re_dealer_master", pd.DataFrame())
        mp = dm.drop_duplicates("dealer_code").set_index("dealer_code") if not dm.empty else pd.DataFrame()
        long = []
        for seg in ("FY", "OY"):
            x = rh[["dealer_code", "dealer_name", "region", "month", "is_mtd", "rm"] + (["zm"] if "zm" in rh else [])].copy()
            x["segment"] = seg; x["base"] = rh[f"{seg.lower()}_base"]; x["ach"] = rh[f"{seg.lower()}_ach"]
            long.append(x)
        x = pd.concat(long)
        if not mp.empty:
            x["rm"] = [r or (mp.at[c, "rm"] if c in mp.index else "") for c, r in zip(x["dealer_code"], x["rm"])]
            x["branch"] = [mp.at[c, "branch"] if c in mp.index else "" for c in x["dealer_code"]]
        else:
            x["branch"] = ""
        x["zm"] = [z if isinstance(z, str) and z else re_zm(r) for r, z in
                   zip(x["region"], x["zm"] if "zm" in x else [""] * len(x))]
        parts.append(x.assign(book="Royal Enfield", oem="ROYAL ENFIELD"))
    cols = ["book", "oem", "dealer_code", "dealer_name", "zm", "rm", "branch", "region", "month", "is_mtd", "segment", "base", "ach"]
    ret = pd.concat([p.reindex(columns=cols) for p in parts], ignore_index=True) if parts else pd.DataFrame(columns=cols)
    for c in ("zm", "rm", "branch", "region", "dealer_name"):
        ret[c] = ret[c].fillna("").astype(str)
    ret["base"] = pd.to_numeric(ret["base"], errors="coerce").fillna(0)
    ret["ach"] = pd.to_numeric(ret["ach"], errors="coerce").fillna(0)
    ret = ret[(ret["base"] > 0) | (ret["ach"] > 0)]
    pack.add("retention", ret)

    # ---------- RE national dealer snapshot (current month, first year) ----------
    inc = t.get("re_incentive", pd.DataFrame())
    if inc.empty and not dmail.empty:
        # no incentive tracker in this drop: the national daily mail gives every dealer's first-year base / renewals
        cur = dmail["month"].max()
        dm_ = dmail[dmail["month"] == cur]
        inc = pd.DataFrame({"dealer_code": dm_["dealer_code"], "dealer_name": dm_["dealer_name"], "dealer_group": dm_["dealer_name"],
                            "region": dm_["region"], "state": dm_["state"], "city": dm_["city"], "rm": dm_["rm"], "zone": "",
                            "jul_base": 0.0, "jul_ach": 0.0, "retention_band": "", "fy_base": dm_["fy_base"], "fy_ach": dm_["fy_ach"],
                            "tier": float("nan"), "incentive_earned": 0.0, "next_tier": float("nan"), "nop_to_next_tier": float("nan"),
                            "target_5": float("nan"), "target_20": float("nan"), "_zm": dm_["zm"]})
    ai = t.get("re_ai_dealer", pd.DataFrame())
    dm = t.get("re_dealer_master", pd.DataFrame())
    if not inc.empty:
        snap = inc.copy()
        snap["zm"] = [z if isinstance(z, str) and z else re_zm(r) for r, z in
                      zip(snap["region"], snap["_zm"] if "_zm" in snap else [""] * len(snap))]
        snap = snap.drop(columns=["_zm"], errors="ignore")
        if not ai.empty:
            snap = snap.merge(ai[["dealer_code", "calls", "disposed", "conversions"]].rename(
                columns={"calls": "ai_calls", "disposed": "ai_disposed", "conversions": "ai_conversions"}),
                on="dealer_code", how="left")
        mtd = t.get("re_dealer_mtd", pd.DataFrame())
        if not mtd.empty:
            snap = snap.merge(mtd[["dealer_code", "mtd_target", "mtd_actual", "mtd_target_prev", "mtd_actual_prev"]],
                              on="dealer_code", how="left")
        vis = t.get("re_visits", pd.DataFrame())
        if not vis.empty:
            last = vis[vis["dealer_code"] != ""].groupby("dealer_code")["date"].agg(["max", "count"])
            snap = snap.merge(last.rename(columns={"max": "last_visit", "count": "visits"}), left_on="dealer_code",
                              right_index=True, how="left")
        foc = t.get("re_focus", pd.DataFrame())
        snap["focus_dealer"] = snap["dealer_code"].isin(set(foc["dealer_code"])) if not foc.empty else False
        pack.add("re_dealer_snapshot", snap)

    # ---------- data-quality checks ----------
    if not ret.empty:
        over = ret[(ret["ach"] > ret["base"]) & (ret["base"] > 0)]
        if len(over):
            pack.note("medium", "retention", f"{over['dealer_code'].nunique()} dealer-months show renewals above base "
                                             "(renewals booked on another code of the same group).")
        blank = ret[ret["zm"] == ""]
        if len(blank):
            pack.note("low", "retention", f"{blank['dealer_code'].nunique()} dealers have no ZM mapping "
                                          f"({', '.join(sorted(blank['oem'].unique()))}).")


# --------------------------------------------------------------------------- #
# dataset catalogue for the agent
# --------------------------------------------------------------------------- #
CATALOG = {
    "retention": "Unified renewal/retention fact. One row per book, oem, dealer, month and segment (FY = first-year renewal, "
                 "OY = other-year). base = policies due, ach = renewed. is_mtd=True marks the running month (partial). "
                 "Private-car OEMs: AUDI, SKODA, VOLKSWAGEN, PORSCHE (Jan-26..current) and VOLVO; ROYAL ENFIELD rows are "
                 "North-zone dealers only (Apr-26..current). Retention % = sum(ach)/sum(base).",
    "pc_penetration_month": "Audi/Skoda/VW new-car penetration and renewal per dealer per month (Jan-26..Aug-26): retail = cars "
                            "sold, new_nop = new policies, penetration = new_nop/retail, renewal_base, renewed, rollover.",
    "pc_payouts": "SAVWIPL + Volvo dealer payout invoices (distribution fees): status, base_value (pending value), net_amount "
                  "(paid), invoice_stage, fy, month, rm, zm. paid=True when status is 'Already Paid'.",
    "volvo_open_cases": "Volvo policies that expired this month and are NOT renewed (chase list, no customer identifiers): "
                        "dealer, insurer, model, cover, segment FY/OY, policy_year, premium, claims, expiry_date, rm, zm.",
    "volvo_p2p_month": "Volvo policy-to-policy retention by dealer for the recent months (base, ach).",
    "volvo_expiry_month": "Volvo FY 2026-27 expiry cohort by dealer, month, segment and EV/Non-EV (base, ach).",
    "re_region": "Royal Enfield region view (current month): FY base/ach now and same day last month, OY base/ach, totals, "
                 "dealer count and AI-enabled dealers.",
    "re_region_mtd": "RE region month-to-date renewals vs same date last month (mtd_target = policies expired so far).",
    "re_daily": "RE renewals per region per calendar day this month.",
    "re_dealer_mtd": "RE dealer MTD target (expired so far) and renewals, vs same date last month; activation growth flag.",
    "re_dealer_snapshot": "RE dealer snapshot (national): first-year base/ach this month, July baseline, incentive tier, "
                          "incentive earned, NOP to next tier, AI calls/conversions, MTD, last visit, focus flag, rm, zm.",
    "re_incentive": "RE dealer incentive tracker: July baseline, targets +5/+10/+15/+20 pts, tier reached, earned, NOP to next tier.",
    "re_ai_dealer": "RE AI calling per dealer this month: calls, failed, disposed (answered & worked), abandoned, conversions.",
    "re_ai_daily": "RE AI calling per day this month vs the same day last month.",
    "re_ai_region": "RE AI calling and consent coverage per region.",
    "re_focus": "RE focus dealers (high base / low first-year conversion) with last-month and current base/ach.",
    "re_penetration": "RE new-vehicle penetration per dealer: vehicles sold vs program policies (prev months, MTD, YTD) and add-on mix.",
    "re_history": "RE North-zone monthly dealer FY/OY base and ach (Apr-26..current).",
    "re_visits": "RE RM field visits: date, rm, dealer, region, persons met, telecaller, topics discussed, support requested.",
    "re_edme": "RE EDME 360 calling app usage per dealer (base loaded, calls MTD, active calling days).",
    "re_payouts": "RE dealer payout invoices: status, base_value, net_amount, fy, month, rm, zm.",
    "re_dealer_master": "RE dealer master: code -> rm, zm, branch, group, city, state, retail volumes.",
}
