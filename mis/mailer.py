"""Dealer mailers: a payout-outstanding mail and a performance mail, each to the dealer with the RM (and optionally the ZM)
in CC.

- Payout mail (bulk, sent from the app): every dealer with one or more unpaid invoices gets the list of their own
  unpaid invoices, the reason each is held and what the dealer has to do.
- Performance mail (automatic, scripts/auto_mailer.py): whenever a newer performance report lands, every dealer in the
  directory gets their own retention (YTD / last month / MTD, first year / other year), penetration and renewals to
  target; Royal Enfield dealers also get their incentive tier. Clubbed groups list each code, then the group total.

Dealer-facing content rules (same as the dealer meeting deck): only that dealer's own numbers - no other dealer names,
no RM visit data, no defaulter labels, no customer data.

Modes: "dry" (build only, nothing sent), "test" (everything goes to MAILER_TEST_TO), "live" (to the dealers).
SMTP settings come from secrets / environment: SMTP_HOST, SMTP_PORT (465 SSL or 587 STARTTLS), SMTP_USER,
SMTP_PASSWORD, MAIL_FROM, MAIL_FROM_NAME, MAIL_REPLY_TO, MAILER_TEST_TO, MAIL_CC_ZM (yes/no).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import re
import smtplib
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

import pandas as pd

from . import groups as G
from .model import OEM_LABEL, TARGETS
from .util import month_label, ratio

esc = html.escape
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
GREEN = "#1F5B3F"

# reason -> what the dealer must do (payout mail)
DEALER_ACTION = [
    (r"not uploaded|num not updated", "Upload the invoice on the portal"),
    (r"discrepanc|re-upload", "Re-upload the invoice with the corrected details"),
    (r"physical", "Send the signed physical invoice"),
    (r"gstin", "Update / reactivate your GSTIN registration and inform your RM"),
    (r"deactivation", "Contact your RM - the dealer code is under deactivation"),
    (r"bank|psu", "Share updated bank details with your RM"),
    (r"branch|ops", "On hold by the branch - your RM will update you"),
    (r"payout on|scheduled", "No action - scheduled for payment"),
]


def action_for(status: str) -> str:
    for rx, a in DEALER_ACTION:
        if re.search(rx, status or "", re.I): return a
    return "Please contact your RM"


# --------------------------------------------------------------------------- #
# directory
# --------------------------------------------------------------------------- #
def _emails(v) -> list[str]:
    return list(dict.fromkeys(EMAIL_RX.findall(str(v or ""))))


def load_directory(path_or_buf) -> pd.DataFrame:
    """Any sheet with a dealer-code column and e-mail columns. Header names are matched loosely:
    code: 'dealer code' / 'code'; dealer mail: 'dealer email' / 'dealer mail' / 'email'; RM: 'rm email' / 'rm mail';
    ZM: 'zm email'. Several addresses in one cell (; or , separated) are fine."""
    xl = pd.read_excel(path_or_buf, sheet_name=None, dtype=str)
    for _, df in xl.items():
        cols = {c: re.sub(r"[^a-z]", "", str(c).lower()) for c in df.columns}
        def find(*keys, avoid=()):
            for c, k in cols.items():
                if any(x in k for x in keys) and not any(a in k for a in avoid): return c
            return None
        code = find("dealercode", "code", "mispcode")
        rm_e = find("rmemail", "rmmail", "rmid")
        zm_e = find("zmemail", "zmmail", "zmid")
        d_e = find("dealeremail", "dealermail", "dealermailid", "email", "mailid", avoid=("rm", "zm"))
        if not code or not d_e: continue
        out = pd.DataFrame({"code": df[code].astype(str).str.strip().str.upper().str.replace(r"\.0$", "", regex=True),
                            "dealer_emails": df[d_e].map(_emails),
                            "rm_emails": df[rm_e].map(_emails) if rm_e else [[] for _ in range(len(df))],
                            "zm_emails": df[zm_e].map(_emails) if zm_e else [[] for _ in range(len(df))]})
        name = find("dealername", "name")
        out["name"] = df[name].astype(str) if name else ""
        return out[out["code"].ne("") & out["code"].ne("NAN")].drop_duplicates("code").reset_index(drop=True)
    raise ValueError("No sheet with a dealer-code column and a dealer e-mail column was found.")


def directory_template(pack) -> bytes:
    """Excel with every dealer code, name, OEM, RM, ZM pre-filled - fill in the e-mail columns and upload it."""
    import io
    rows = []
    r = pack.get("retention")
    if not r.empty:
        x = r[r["book"] == "Private car"].drop_duplicates(["oem", "dealer_code"])
        rows += [{"Dealer code": c, "Dealer name": n, "OEM": OEM_LABEL.get(o, o), "RM": rm, "ZM": zm}
                 for o, c, n, rm, zm in x[["oem", "dealer_code", "dealer_name", "rm", "zm"]].itertuples(index=False)]
    s = pack.get("re_dealer_snapshot")
    if not s.empty:
        rows += [{"Dealer code": c, "Dealer name": n, "OEM": "Royal Enfield", "RM": rm, "ZM": zm}
                 for c, n, rm, zm in s[["dealer_code", "dealer_name", "rm", "zm"]].itertuples(index=False)]
    df = pd.DataFrame(rows).drop_duplicates("Dealer code")
    for c in ("Dealer email", "RM email", "ZM email"): df[c] = ""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="Directory", index=False)
        pd.DataFrame({"How to fill": [
            "One row per dealer code. Several addresses in one cell: separate with ;",
            "Dealer email = To. RM email = CC. ZM email = CC only when 'CC the ZM' is switched on.",
            "Upload the file in the app (Mail tab) or put it in the Google Drive MIS folder with 'Dealer Directory' in its name."]
        }).to_excel(w, sheet_name="Read me", index=False)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# mail building blocks
# --------------------------------------------------------------------------- #
def _pct(v):
    return "–" if v is None or v != v else f"{v * 100:.1f}%"


def _n(v):
    try: return f"{float(v):,.0f}"
    except (TypeError, ValueError): return "–"


def _inr(v):
    v = float(v or 0)
    return f"₹{v / 1e5:,.2f} L" if abs(v) >= 1e5 else f"₹{v:,.0f}"


def _table(head: list[str], rows: list[list], right_from=1) -> str:
    th = "".join(f"<th style='text-align:{'left' if i < right_from else 'right'};padding:6px 10px;background:{GREEN};"
                 f"color:#fff;font-weight:600;font-size:13px'>{esc(h)}</th>" for i, h in enumerate(head))
    trs = []
    for k, r in enumerate(rows):
        bold = str(r[0]).startswith("Σ")
        tds = "".join(f"<td style='text-align:{'left' if i < right_from else 'right'};padding:6px 10px;border-bottom:1px solid #E6EAEE;"
                      f"font-size:13px;{'font-weight:700;background:#F6F8F9;' if bold else ''}'>{esc(str(v))}</td>" for i, v in enumerate(r))
        trs.append(f"<tr>{tds}</tr>")
    return f"<table style='border-collapse:collapse;width:100%;margin:8px 0 14px'><tr>{th}</tr>{''.join(trs)}</table>"


def _wrap(title: str, inner: str, footer: str) -> str:
    return (f"<div style='font-family:Arial,Helvetica,sans-serif;color:#14202B;max-width:720px'>"
            f"<div style='border-left:4px solid {GREEN};padding:4px 12px;margin-bottom:12px'><div style='font-size:18px;font-weight:700'>"
            f"{esc(title)}</div></div>{inner}<p style='font-size:12px;color:#6B7A88;margin-top:18px'>{footer}</p></div>")


def _text_from_html(h: str) -> str:
    t = re.sub(r"<(br|/p|/tr|/div|/h\d)>", "\n", h)
    t = re.sub(r"</t[dh]>", "\t", t)
    return html.unescape(re.sub(r"<[^>]+>", "", t)).strip()


# --------------------------------------------------------------------------- #
# payout mail
# --------------------------------------------------------------------------- #
def unpaid_for(pack, code: str) -> pd.DataFrame:
    parts = []
    for t in ("re_payouts", "pc_payouts"):
        d = pack.get(t)
        if not d.empty:
            parts.append(d[(~d["paid"]) & (d["dealer_code"].astype(str).str.upper() == code)])
    if not parts: return pd.DataFrame()
    u = pd.concat(parts, ignore_index=True)
    if u.empty: return u
    inv = u["invoice_no"].astype(str).str.strip() if "invoice_no" in u else pd.Series("", index=u.index)
    u = u.assign(inv=inv.where(~inv.isin(["", "0", "nan", "None"]), "(invoice no. not updated)"))
    good = u[u["inv"] != "(invoice no. not updated)"].groupby("inv", as_index=False).agg(
        month=("month", "min"), fy=("fy", "first"), value=("base_value", "sum"), status=("status", "first"), oem=("oem", "first"))
    bad = u[u["inv"] == "(invoice no. not updated)"].rename(columns={"base_value": "value"})[["inv", "month", "fy", "value", "status", "oem"]]
    return pd.concat([good, bad], ignore_index=True).sort_values("month")


def payout_dealers(pack, zm=None, rm=None, oem=None) -> pd.DataFrame:
    """Dealers with >= 1 unpaid invoice (any reason)."""
    parts = []
    for t in ("re_payouts", "pc_payouts"):
        d = pack.get(t)
        if d.empty: continue
        d = d[~d["paid"]]
        if oem and oem != "OVERALL": d = d[d["oem"] == oem]
        if zm: d = d[d["zm"] == zm]
        if rm: d = d[d["rm"] == rm]
        parts.append(d)
    if not parts: return pd.DataFrame()
    u = pd.concat(parts)
    g = u.groupby(u["dealer_code"].astype(str).str.upper()).agg(name=("dealer_name", "first"), oem=("oem", "first"), rm=("rm", "first"),
                                                                 zm=("zm", "first"), lines=("status", "size"), value=("base_value", "sum"))
    return g.reset_index().rename(columns={"dealer_code": "code"})


def payout_mail(pack, code: str, name: str = "") -> dict | None:
    u = unpaid_for(pack, code)
    if u.empty: return None
    if not name:
        for t in ("pc_payouts", "re_payouts"):
            d = pack.get(t)
            hit = d[d["dealer_code"].astype(str).str.upper() == code] if not d.empty else d
            if len(hit): name = str(hit["dealer_name"].iloc[0]).strip(); break
    name = name or code
    total = u["value"].sum()
    rows = [[r.inv, month_label(r.month) if isinstance(r.month, str) else "", r.fy, _inr(r.value), r.status, action_for(r.status)]
            for r in u.itertuples()]
    acts = sorted({action_for(s) for s in u["status"]} - {"No action - scheduled for payment"})
    inner = (f"<p>Dear Partner,</p><p>Our records show <b>{len(u)} invoice{'s' if len(u) > 1 else ''}</b> for <b>{esc(name)} ({esc(code)})</b> "
             f"with distribution payout still pending - total <b>{_inr(total)}</b> (base value).</p>"
             + _table(["Invoice", "Month", "FY", "Base value", "Status", "Action needed"], rows, right_from=3)
             + ("<p><b>To release these payouts:</b></p><ul>" + "".join(f"<li>{esc(a)}</li>" for a in acts) + "</ul>" if acts else "")
             + "<p>Your RM (in copy) will help you close these. Please reply to this mail once done.</p>")
    subj = f"{code} {name} - payout pending on {len(u)} invoice{'s' if len(u) > 1 else ''} ({_inr(total)})"
    h = _wrap(f"Payout status - {name}", inner, f"EIBL · status as per the payout (UTR) file of {dt.date.today():%d %b %Y}. "
              "This is an automated mail; figures are base values before tax.")
    return {"kind": "payout", "code": code, "subject": subj, "html": h, "text": _text_from_html(h), "invoices": len(u), "value": total}


# --------------------------------------------------------------------------- #
# performance mail
# --------------------------------------------------------------------------- #
def _car_lines(pack, oem, codes: list[str]) -> list[dict]:
    from .mgmt import pen_by
    from .views import ytd_mtd_table
    t = ytd_mtd_table(pack, oem, ["dealer_code", "dealer_name"])
    if t.empty: return []
    t = t[t["dealer_code"].isin(codes)]
    pn = pen_by(pack, oem, "dealer_code")
    lm = next((c for c in t.columns if c.endswith(" %") and c not in ("YTD %", "FY YTD %", "OY YTD %", "Target %") and not c.startswith("MTD")), None)
    mt = next((c for c in t.columns if c.startswith("MTD")), None)
    out = []
    for r in t.itertuples(index=False):
        d = r._asdict() if hasattr(r, "_asdict") else dict(zip(t.columns, r))
        row = dict(zip(t.columns, r))
        out.append({"code": row["dealer_code"], "name": row["dealer_name"], "base": row["YTD base"], "ren": row["YTD renewed"],
                    "ytd": row["YTD %"], "fy": row["FY YTD %"], "oy": row["OY YTD %"], "lm": row.get(lm), "mtd": row.get(mt),
                    "lm_label": lm.replace(" %", "") if lm else "Last month", "mtd_label": mt.replace(" %", "") if mt else "MTD",
                    "sold": pn["Vehicles sold"].get(row["dealer_code"]) if len(pn) else None,
                    "pol": pn["Policies"].get(row["dealer_code"]) if len(pn) else None,
                    "pen": pn["Pen YTD %"].get(row["dealer_code"]) if len(pn) else None})
    return out


def performance_mail(pack, code: str, name: str = "") -> dict | None:
    """This dealer's own retention and penetration; a clubbed group lists each code, then the group total."""
    r = pack.get("retention")
    snap = pack.get("re_dealer_snapshot")
    footer = ("EIBL · retention = policies renewed / policies due for renewal; penetration = policies sold with the vehicle / "
              "vehicles sold. This is an automated mail - reply to reach your RM.")
    # Royal Enfield
    if not snap.empty and (snap["dealer_code"].astype(str).str.upper() == code).any():
        x = snap[snap["dealer_code"].astype(str).str.upper() == code].iloc[0]
        t = TARGETS["ROYAL ENFIELD"]
        fy = ratio(x["fy_ach"], x["fy_base"])
        prev = ratio(x.get("mtd_actual_prev"), x.get("mtd_target_prev")) if x.get("mtd_target_prev") else None
        need = max(0, int(-(-(t["FY"] * x["fy_base"] - x["fy_ach"]) // 1))) if x["fy_base"] else 0
        pen = pack.get("re_penetration")
        pr = pen[pen["dealer_code"].astype(str).str.upper() == code] if not pen.empty else pen
        rows = [["First-year renewals this month", f"{_n(x['fy_ach'])} of {_n(x['fy_base'])}", _pct(fy)],
                ["Target (first year)", "", _pct(t["FY"])],
                ["Renewals still needed for target", _n(need), ""]]
        if prev is not None: rows.append(["Same date last month", "", _pct(prev)])
        if len(pr):
            p = pr.iloc[0]
            rows += [["Penetration this month (policies / bikes sold)", f"{_n(p['prog_mtd'])} / {_n(p['veh_mtd'])}", _pct(ratio(p["prog_mtd"], p["veh_mtd"]))],
                     ["Penetration year to date", f"{_n(p['prog_ytd'])} / {_n(p['veh_ytd'])}", _pct(ratio(p["prog_ytd"], p["veh_ytd"]))]]
        inc = ""
        if x.get("next_tier") == x.get("next_tier") and x.get("nop_to_next_tier") == x.get("nop_to_next_tier") and (x.get("nop_to_next_tier") or 0) > 0:
            inc = (f"<p><b>Incentive:</b> current tier {_n(x.get('tier'))}; <b>{_n(x['nop_to_next_tier'])} more first-year renewals</b> "
                   f"take you to the next tier ({_n(x['next_tier'])} pts above your July baseline).</p>")
        nm = name or x["dealer_name"]
        inner = (f"<p>Dear Partner,</p><p>Here is <b>{esc(nm)} ({esc(code)})</b>'s Royal Enfield RESP renewal performance as of "
                 f"<b>{esc(pack.asof.get('Royal Enfield', ''))}</b>.</p>" + _table(["Measure", "Policies", "%"], rows) + inc
                 + "<p>First-year customers renew best when called 30-45 days before expiry. Your RM (in copy) will work with you on the list.</p>")
        h = _wrap(f"Renewal performance - {nm}", inner, footer)
        return {"kind": "performance", "code": code, "subject": f"{code} {nm} - RESP renewal performance {_pct(fy)} vs {_pct(t['FY'])} target",
                "html": h, "text": _text_from_html(h), "oem": "ROYAL ENFIELD"}
    # private car
    hit = r[(r["dealer_code"].astype(str).str.upper() == code) & (r["book"] == "Private car")]
    if hit.empty: return None
    oem = hit["oem"].iloc[0]
    nm = name or hit["dealer_name"].iloc[0]
    grp = G.group_of(nm, oem)
    codes = [code]
    if grp:
        same = r[(r["oem"] == oem)].drop_duplicates("dealer_code")
        codes = [c for c, n in zip(same["dealer_code"], same["dealer_name"]) if G.group_of(n, oem) == grp] or [code]
    lines = _car_lines(pack, oem, codes)
    if not lines: return None
    t = TARGETS.get(oem, TARGETS["SKODA"])
    L = lines[0]
    head = ["Code", "Dealer", "Retention YTD", "First year", "Other year", L["lm_label"], L["mtd_label"], "Penetration YTD"]
    rows = [[x["code"], x["name"], _pct(x["ytd"]), _pct(x["fy"]), _pct(x["oy"]), _pct(x["lm"]), _pct(x["mtd"]), _pct(x["pen"])] for x in lines]
    if len(lines) > 1:
        B = sum(x["base"] for x in lines); R = sum(x["ren"] for x in lines)
        S_ = sum(x["sold"] or 0 for x in lines); P = sum(x["pol"] or 0 for x in lines)
        rows.append([f"Σ {grp} group", f"{len(lines)} codes", _pct(ratio(R, B)), "", "", "", "", _pct(ratio(P, S_) if S_ else None)])
        tot_ret, tot_base, tot_ren = ratio(R, B), B, R
    else:
        tot_ret, tot_base, tot_ren = L["ytd"], L["base"], L["ren"]
    need = max(0, int(-(-(t["TOT"] * tot_base - tot_ren) // 1)))
    who = f"the {grp} group" if len(lines) > 1 else f"{nm} ({code})"
    inner = (f"<p>Dear Partner,</p><p>Here is {esc(who)}'s {OEM_LABEL.get(oem, oem)} insurance renewal performance, year to date "
             f"(MIS as of <b>{esc(pack.asof.get('Private car', ''))}</b>).</p>"
             + _table(head, rows, right_from=2)
             + f"<p>Retention target: <b>{_pct(t['TOT'])}</b> (first year {_pct(t['FY'])}, other year {_pct(t['OY'])}). "
             + (f"<b>{_n(need)} more renewals</b> this year bring {esc(who)} to target.</p>" if need else "You are at or above target - thank you.</p>")
             + "<p>Your RM (in copy) will share the list of customers due for renewal.</p>")
    h = _wrap(f"Renewal performance - {grp + ' group' if len(lines) > 1 else nm}", inner, footer)
    return {"kind": "performance", "code": code, "subject": f"{code} {nm} - {OEM_LABEL.get(oem, oem)} renewal performance YTD {_pct(tot_ret)} vs {_pct(t['TOT'])} target",
            "html": h, "text": _text_from_html(h), "oem": oem}


# --------------------------------------------------------------------------- #
# recipients, sending, log
# --------------------------------------------------------------------------- #
def recipients(directory: pd.DataFrame, code: str, cc_zm=False) -> tuple[list, list]:
    row = directory[directory["code"] == code]
    if row.empty: return [], []
    r = row.iloc[0]
    to = list(r["dealer_emails"])
    cc = list(dict.fromkeys(list(r["rm_emails"]) + (list(r["zm_emails"]) if cc_zm else [])))
    return to, [c for c in cc if c not in to]


def smtp_config(get) -> dict:
    """get(key, default) reads secrets / env."""
    return {"host": get("SMTP_HOST", "smtp.gmail.com"), "port": int(get("SMTP_PORT", 465) or 465), "user": get("SMTP_USER", ""),
            "password": get("SMTP_PASSWORD", ""), "from": get("MAIL_FROM", "") or get("SMTP_USER", ""),
            "from_name": get("MAIL_FROM_NAME", "EIBL Renewals"), "reply_to": get("MAIL_REPLY_TO", ""),
            "test_to": get("MAILER_TEST_TO", ""), "cc_zm": str(get("MAIL_CC_ZM", "no")).lower() in ("1", "yes", "true")}


def send(mails: list[dict], cfg: dict, mode: str = "dry", pause: float = 0.6, log_path: str | None = None, progress=None) -> pd.DataFrame:
    """mails: dicts with subject / html / text / to / cc (+ attachments [(name, bytes, mime)]).
    mode: dry = nothing sent; test = all to cfg['test_to'] (no CC); live = real recipients."""
    log = []
    server = None
    if mode in ("test", "live"):
        if not cfg.get("user") or not cfg.get("password"):
            raise ValueError("SMTP_USER and SMTP_PASSWORD are not set in the app secrets.")
        if mode == "test" and not cfg.get("test_to"):
            raise ValueError("MAILER_TEST_TO is not set - test mode needs your own address.")
        server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=60) if cfg["port"] == 465 else smtplib.SMTP(cfg["host"], cfg["port"], timeout=60)
        if cfg["port"] != 465: server.starttls()
        server.login(cfg["user"], cfg["password"])
    try:
        for i, m in enumerate(mails):
            if progress: progress(i / max(1, len(mails)), m["code"])
            to, cc = (m["to"], m["cc"]) if mode == "live" else ([cfg.get("test_to")] if mode == "test" else m["to"], [] if mode == "test" else m["cc"])
            rec = {"time": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "kind": m["kind"], "code": m["code"], "to": "; ".join(to),
                   "cc": "; ".join(cc), "subject": m["subject"], "mode": mode, "status": ""}
            if not to:
                rec["status"] = "skipped - no dealer e-mail in the directory"; log.append(rec); continue
            if mode == "dry":
                rec["status"] = "built (dry run - not sent)"; log.append(rec); continue
            msg = EmailMessage()
            msg["Subject"] = ("[TEST] " if mode == "test" else "") + m["subject"]
            msg["From"] = formataddr((cfg["from_name"], cfg["from"]))
            msg["To"] = ", ".join(to)
            if cc: msg["Cc"] = ", ".join(cc)
            if cfg.get("reply_to"): msg["Reply-To"] = cfg["reply_to"]
            msg["Message-ID"] = make_msgid()
            msg.set_content(m["text"])
            msg.add_alternative(m["html"], subtype="html")
            for name, data, mime in m.get("attachments", []):
                mt, st = mime.split("/", 1)
                msg.add_attachment(data, maintype=mt, subtype=st, filename=name)
            try:
                server.send_message(msg)
                rec["status"] = "sent"
            except Exception as e:
                rec["status"] = f"failed: {type(e).__name__}: {str(e)[:120]}"
            log.append(rec)
            time.sleep(pause)
    finally:
        if server:
            try: server.quit()
            except Exception: pass
    df = pd.DataFrame(log)
    if log_path and len(df):
        df.to_csv(log_path, mode="a", header=not os.path.exists(log_path), index=False)
    return df


def build(pack, directory: pd.DataFrame, kind: str, codes: list[str], cc_zm=False) -> list[dict]:
    out = []
    names = dict(zip(directory["code"], directory.get("name", pd.Series("", index=directory.index)))) if len(directory) else {}
    for c in codes:
        m = payout_mail(pack, c, "") if kind == "payout" else performance_mail(pack, c, "")
        if not m: continue
        m["to"], m["cc"] = recipients(directory, c, cc_zm)
        out.append(m)
    return out


def report_signature(pack) -> str:
    """Changes whenever a performance file changes (new drop / new as-of date)."""
    keys = ("pc_renewal", "volvo_summary", "volvo_daily", "re_renewal", "re_incentive", "re_history", "re_pen", "savwipl_pen")
    sig = {k: (f.name, str(f.asof)) for k, f in pack.sources.items() if k in keys}
    return hashlib.md5(json.dumps(sig, sort_keys=True).encode()).hexdigest()[:16]
