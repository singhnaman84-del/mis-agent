"""MIS Retention Analyst - all OEMs. Layout and theme follow the RESP Retention Analyst: top bar with chips, KPI strip,
tabbed main panel + Claude chat panel on the right. Click any dealer / RM / ZM row to open its dashboard; every
dashboard, lookup and answer exports to HTML / PDF / PPT / CSV.

Run locally:   streamlit run app.py        Deploy: see README.md
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import re
import tempfile
import time

import pandas as pd
import streamlit as st

from mis import analytics as A
from mis import ra
from mis.agent import DEFAULT_MODEL, EFFORTS, PROVIDERS, run_agent
from mis.charts import BRASS, to_plotly
from mis.loaders import FILE_TYPES, load_folder
from mis.model import CATALOG, OEM_LABEL, OEMS, TARGETS, re_zm
from mis.report import PCT_HINT, Report, export, fmt_cell

st.set_page_config(page_title="MIS Retention Analyst", page_icon="📊", layout="wide", initial_sidebar_state="collapsed")

# --------------------------------------------------------------------------- #
# theme (RESP Retention Analyst tokens)
# --------------------------------------------------------------------------- #
st.markdown("""
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root{
  --ground:#EEF1F4; --surface:#FFFFFF; --surface-2:#F6F8F9; --line:#D5DBE0; --line-soft:#E6EAEE;
  --ink:#14202B; --ink-2:#3E4C59; --ink-3:#6B7A88;
  --accent:#1F5B3F; --accent-ink:#FFFFFF; --accent-soft:#E3EFE8; --brass:#B08A3C; --brass-soft:#F4EBD6;
  --bad:#B3261E; --bad-soft:#F9E5E3; --warn:#9A6B00; --warn-soft:#FBF1D6; --good:#1F5B3F;
  --shadow:0 1px 2px rgba(20,32,43,.06),0 8px 24px -12px rgba(20,32,43,.18);
  --radius:10px; --font-d:"Archivo",system-ui,sans-serif; --font-b:"IBM Plex Sans",system-ui,sans-serif;
}
/* streamlit chrome off */
header[data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stDecoration"], footer, #MainMenu,
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"], [data-testid="stStatusWidget"] {display:none !important}
.stApp{background:var(--ground)}
html, body, .stApp, .stMarkdown, p, li, label, input, textarea, select, button, [data-testid="stWidgetLabel"]{font-family:var(--font-b) !important}
h1,h2,h3,h4{font-family:var(--font-d) !important;color:var(--ink)}
.block-container{max-width:1480px;padding:0 16px 32px !important}
.num{font-variant-numeric:tabular-nums}
/* top bar */
.top{background:var(--surface);border:1px solid var(--line);border-top:0;border-radius:0 0 var(--radius) var(--radius);
  display:flex;align-items:center;gap:16px;padding:12px 16px;flex-wrap:wrap;margin:0 0 4px}
.brand{display:flex;align-items:center;gap:12px}
.brand .mark{width:34px;height:34px;border-radius:8px;background:var(--accent);color:var(--accent-ink);display:grid;place-items:center;
  font-family:var(--font-d);font-weight:700;font-size:13px;letter-spacing:.5px}
.brand h1{font-size:17px !important;font-weight:600;letter-spacing:.2px;margin:0 !important;padding:0 !important}
.brand p{margin:0;font-size:12px;color:var(--ink-3)}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-left:auto}
.chip{font-size:12px;padding:4px 10px;border-radius:999px;border:1px solid var(--line);background:var(--surface-2);color:var(--ink-2);white-space:nowrap}
.chip b{color:var(--ink);font-weight:600}
/* kpi strip */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin:12px 0 4px}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px;display:grid;gap:6px;box-shadow:var(--shadow)}
.kpi .lbl{font-family:var(--font-d);font-size:11px;letter-spacing:1.2px;text-transform:uppercase;color:var(--ink-3);font-weight:600}
.kpi .val{font-family:var(--font-d);font-size:36px;line-height:1;font-weight:700;letter-spacing:-.5px;color:var(--ink)}
.kpi .val.bad{color:var(--bad)}
.kpi .sub{font-size:12.5px;color:var(--ink-2)}
.kpi .bar{height:6px;background:var(--line-soft);border-radius:3px;position:relative;overflow:hidden;margin-top:2px}
.kpi .bar i{position:absolute;left:0;top:0;bottom:0;background:var(--accent);border-radius:3px}
.kpi .bar em{position:absolute;top:-2px;bottom:-2px;width:2px;background:var(--brass)}
.kpi .foot{display:flex;justify-content:space-between;font-size:12px;color:var(--ink-3)}
/* panels = bordered containers */
.st-key-mainpanel, .st-key-chatpanel{background:var(--surface) !important;border:1px solid var(--line) !important;
  border-radius:var(--radius) !important;box-shadow:var(--shadow);padding:14px 16px !important}
.st-key-chatpanel{position:sticky;top:12px}
.lead, .lead p{font-size:13px !important;color:var(--ink-2) !important;margin:0 0 8px !important}
.note, .note p{font-size:12.5px !important;color:var(--ink-3) !important;line-height:1.5}
.stMarkdown p, .stMarkdown li{font-size:13.5px}
/* tabs */
[data-baseweb="tab-list"]{gap:2px;border-bottom:1px solid var(--line);overflow-x:auto}
[data-baseweb="tab"]{background:none !important;padding:10px 14px !important;border-radius:8px 8px 0 0;height:auto !important}
[data-baseweb="tab"] p{color:var(--ink-3);font-weight:500;font-size:14px}
[data-baseweb="tab"][aria-selected="true"] p{color:var(--ink);font-weight:600}
[data-baseweb="tab-highlight"]{background:var(--accent) !important;height:2px !important}
[data-baseweb="tab-border"]{display:none}
/* section heads, lead, insight, tiles */
.view-h{font-family:var(--font-d);font-size:16px;font-weight:600;color:var(--ink);margin:2px 0 2px}
.lead{margin:0 0 6px;color:var(--ink-2);font-size:13px}
.sec-h{font-family:var(--font-d);font-size:13px;font-weight:600;letter-spacing:.8px;text-transform:uppercase;color:var(--ink-3);margin:18px 0 6px}
.ins{display:grid;gap:6px;margin:4px 0}
.ins div{padding:8px 10px;border-radius:6px;background:var(--surface-2);border-left:3px solid var(--accent);font-size:13px;color:var(--ink)}
.ins div.flag{border-left-color:var(--bad)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:4px 0 6px}
.tile{background:var(--surface-2);border-radius:8px;padding:12px 14px;display:grid;gap:2px}
.tile .l{font-size:11px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.6px;font-family:var(--font-d)}
.tile .v{font-family:var(--font-d);font-size:24px;font-weight:700;line-height:1.1;color:var(--ink)}
.tile .v.bad{color:var(--bad)}
.tile .s{font-size:12px;color:var(--ink-2)}
/* bar rows */
.bars{display:grid;gap:6px;margin:6px 0 4px}
.btitle{font-family:var(--font-d);font-size:13px;font-weight:600;color:var(--ink);margin:10px 0 2px}
.brow{display:grid;grid-template-columns:190px 1fr 90px;gap:10px;align-items:center;font-size:13px;color:var(--ink)}
.brow .n{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.brow .t{position:relative;height:14px;background:var(--line-soft);border-radius:0 4px 4px 0}
.brow .t i{position:absolute;left:0;top:0;bottom:0;background:var(--accent);border-radius:0 4px 4px 0}
.brow .t i.lo{background:var(--bad)}
.brow .t em{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--brass)}
.brow .v{color:var(--ink-2);text-align:right}
.legend{display:flex;gap:16px;font-size:12px;color:var(--ink-3);margin-top:4px;flex-wrap:wrap}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;vertical-align:-1px;margin-right:6px}
/* focus (drawer-like) header */
.focus{border-left:3px solid var(--brass);padding:2px 0 2px 12px;margin:4px 0 6px}
.focus .k{font-size:11px;letter-spacing:1px;text-transform:uppercase;color:var(--brass);font-family:var(--font-d);font-weight:600}
/* buttons */
.stButton button, .stDownloadButton button, [data-testid="stPopover"] button{border:1px solid var(--line) !important;background:var(--surface) !important;
  color:var(--ink) !important;border-radius:8px !important;font-weight:500 !important;padding:4px 10px !important;min-height:32px !important}
.stButton button:hover, .stDownloadButton button:hover{background:var(--surface-2) !important;border-color:var(--accent) !important}
.stButton button[kind="primary"]{background:var(--accent) !important;color:var(--accent-ink) !important;border-color:var(--accent) !important}
.stButton button p, .stDownloadButton button p{font-size:12.5px !important}
/* inputs */
[data-baseweb="select"] > div, .stTextInput input, [data-baseweb="input"]{border-radius:8px !important;border-color:var(--line) !important}
/* chat */
.chead{display:flex;align-items:center;gap:10px;padding:2px 0 8px;border-bottom:1px solid var(--line);margin-bottom:6px}
.chead .dot{width:8px;height:8px;border-radius:50%;background:var(--accent)}
.chead .dot.off{background:var(--ink-3)}
.chead h2{font-size:15px !important;font-weight:600;margin:0 !important;padding:0 !important}
.chead p{margin:0;font-size:12px;color:var(--ink-3)}
[data-testid="stChatMessage"]{background:transparent;padding:4px 0}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]){background:var(--accent-soft);border-radius:12px 12px 2px 12px;padding:6px 10px}
[data-testid="stChatMessage"] p, [data-testid="stChatMessage"] li{font-size:13.5px}
.note{font-size:12px;color:var(--ink-3);padding-top:6px}
/* data quality */
.dq{display:grid;gap:8px}
.dq div{padding:10px 12px;border-radius:8px;background:var(--surface-2);border-left:3px solid var(--warn);font-size:13px;color:var(--ink)}
.dq div.high{border-left-color:var(--bad)} .dq div.low, .dq div.info{border-left-color:var(--line)}
.dq b{display:block;margin-bottom:2px}
[data-testid="stDataFrame"]{border:1px solid var(--line-soft);border-radius:8px}
</style>""", unsafe_allow_html=True)

esc = html.escape


def secret(key, default=None):
    try:
        return st.secrets[key]
    except Exception:
        return os.environ.get(key, default)


# --------------------------------------------------------------------------- #
# login
# --------------------------------------------------------------------------- #
def login_gate():
    pw = secret("APP_PASSWORD")
    if not pw or st.session_state.get("authed"):
        return
    _, mid, _ = st.columns([1, 1.1, 1])
    with mid:
        st.markdown("<div style='height:12vh'></div><div class='top' style='border-radius:10px;border-top:1px solid var(--line)'>"
                    "<div class='brand'><div class='mark'>MIS</div><div><h1>MIS Retention Analyst</h1>"
                    "<p>Sign in to continue</p></div></div></div>", unsafe_allow_html=True)
        with st.form("login", border=True):
            p = st.text_input("Password", type="password")
            if st.form_submit_button("Sign in", type="primary", width="stretch"):
                if p.strip() == str(pw).strip():
                    st.session_state["authed"] = True
                    st.rerun()
                st.error("Wrong password")
    st.stop()


login_gate()

# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
DATA_DIR = secret("DATA_DIR") or os.path.join(tempfile.gettempdir(), "mis_agent_data")
os.makedirs(DATA_DIR, exist_ok=True)
S = st.session_state


def folder_sig(folder):
    items = sorted((f, os.path.getsize(os.path.join(folder, f)), os.path.getmtime(os.path.join(folder, f)))
                   for f in os.listdir(folder) if f.lower().endswith((".xlsx", ".xlsb", ".xlsm")))
    return hashlib.md5(json.dumps(items).encode()).hexdigest()


@st.cache_resource(show_spinner=False, max_entries=3)
def get_pack(folder, sig):
    return load_folder(folder)


def sa_info():
    try:
        return dict(st.secrets["gcp_service_account"])
    except Exception:
        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        return json.loads(raw) if raw else None


def drive_sync():
    from mis.drive import sync
    bar = st.progress(0.0, "Connecting to Google Drive…")
    try:
        got = sync(sa_info(), DATA_DIR, secret("DRIVE_FOLDER_ID"), progress=lambda f, n: bar.progress(f, f"Downloading {n[:48]}…"))
        S["last_sync"] = time.strftime("%d %b %H:%M")
        st.toast(f"Synced {len(got)} files from Google Drive")
    except Exception as e:
        st.error(f"Drive sync failed: {type(e).__name__}: {e}")
    bar.empty()


def data_controls():
    st.markdown("<div class='sec-h'>Data source</div>", unsafe_allow_html=True)
    c1, c2 = st.columns([1, 1])
    with c1:
        if sa_info():
            if st.button("🔄 Sync latest MIS from Google Drive", type="primary", width="stretch"):
                drive_sync(); st.rerun()
            st.caption(f"Last sync {S.get('last_sync', '—')} · newest file of each type is used")
        else:
            st.caption("Google Drive sync is off - add a service account to the app secrets (see README). Upload files instead.")
    with c2:
        up = st.file_uploader("Upload MIS workbooks", type=["xlsx", "xlsb", "xlsm"], accept_multiple_files=True, label_visibility="collapsed")
        if up:
            for f in up:
                with open(os.path.join(DATA_DIR, f.name), "wb") as fh:
                    fh.write(f.getbuffer())
            st.toast(f"{len(up)} files saved"); st.rerun()


files_present = [f for f in os.listdir(DATA_DIR) if f.lower().endswith((".xlsx", ".xlsb", ".xlsm"))]
if not files_present and sa_info() and "auto_synced" not in S:
    S["auto_synced"] = True
    drive_sync()
    files_present = [f for f in os.listdir(DATA_DIR) if f.lower().endswith((".xlsx", ".xlsb", ".xlsm"))]
if not files_present:
    st.markdown("<div class='top'><div class='brand'><div class='mark'>MIS</div><div><h1>MIS Retention Analyst</h1>"
                "<p>No MIS files loaded yet</p></div></div></div>", unsafe_allow_html=True)
    with st.container(border=True):
        data_controls()
    st.stop()

DATA_SIG = folder_sig(DATA_DIR)
with st.spinner("Reading MIS workbooks… (first load ~30-60 s, then cached)"):
    pack = get_pack(DATA_DIR, DATA_SIG)

# settings live in session state (targets, Claude)
S.setdefault("targets", json.loads(secret("TARGETS_JSON", "null") or "null") or json.loads(json.dumps(TARGETS)))
for o in OEMS:
    TARGETS[o].update(S["targets"][o])
S.setdefault("amber", 10); S.setdefault("min_base", 10); S.setdefault("zm", "All zones")
A.set_criteria(S["amber"] / 100, S["min_base"])
S.setdefault("provider", secret("LLM_PROVIDER", "gemini" if secret("LLM_API_KEY") or secret("GEMINI_API_KEY") else "claude"))
S.setdefault("model", secret("CLAUDE_MODEL", DEFAULT_MODEL))
S.setdefault("effort", secret("CLAUDE_EFFORT", "high"))
API_KEY = S.get("api_key") or (secret("ANTHROPIC_API_KEY") if S["provider"] == "claude" else
                               (secret("LLM_API_KEY") or secret(f"{S['provider'].upper()}_API_KEY"))) or ""
S.setdefault("pins", []); S.setdefault("chat", []); S.setdefault("exports", {}); S.setdefault("handled", {})


# --------------------------------------------------------------------------- #
# rendering helpers
# --------------------------------------------------------------------------- #
def fmt_date(s):
    try:
        return dt.date.fromisoformat(str(s)[:10]).strftime("%d %b %Y")
    except ValueError:
        return str(s)


def entity_of(df: pd.DataFrame):
    """Which entity a table's rows are: dealer (has Code), RM, or ZM - and the column holding the id."""
    cols = {c.lower(): c for c in df.columns}
    for c in ("code", "dealer_code"):
        if c in cols: return "dealer", cols[c]
    for c in ("rm",):
        if c in cols: return "rm", cols[c]
    for c in ("zm",):
        if c in cols: return "zm", cols[c]
    return None, None


RAG_ICON = {"Red": "🔴 Red", "Amber": "🟠 Amber", "Green": "🟢 Green"}


def show_table(df: pd.DataFrame, key: str, title: str = ""):
    if title: st.markdown(f"<div class='btitle'>{esc(title)}</div>", unsafe_allow_html=True)
    d = df.copy()
    cfg = {}
    for c in d.columns:
        s = d[c]
        if c == "RAG":
            d[c] = s.map(lambda v: RAG_ICON.get(v, v))
        elif pd.api.types.is_bool_dtype(s):
            d[c] = s.map({True: "Yes", False: "No"})
        elif pd.api.types.is_datetime64_any_dtype(s):
            cfg[c] = st.column_config.DateColumn(c, format="DD MMM YYYY")
        elif pd.api.types.is_numeric_dtype(s):
            v = pd.to_numeric(s, errors="coerce")
            if PCT_HINT.search(str(c)) and v.dropna().abs().max() <= 5 and "pts" not in c.lower():
                d[c] = v * 100; cfg[c] = st.column_config.NumberColumn(c, format="%.1f%%")
            elif "pts" in c.lower():
                cfg[c] = st.column_config.NumberColumn(c, format="%+.1f")
            elif (v.dropna() % 1 != 0).any() and v.abs().max() < 1000:
                cfg[c] = st.column_config.NumberColumn(c, format="%.2f")
            else:
                cfg[c] = st.column_config.NumberColumn(c, format="localized")
    kind, idcol = entity_of(d)
    h = min(440, 38 + 35 * len(d))
    if kind:
        ev = st.dataframe(d, hide_index=True, width="stretch", height=h, column_config=cfg,
                          on_select="rerun", selection_mode="single-row", key=f"tb{key}")
        rows = ev.selection.rows if ev and hasattr(ev, "selection") else []
        if rows:
            val = str(df.iloc[rows[0]][idcol])
            token = f"{rows[0]}:{val}"
            if S["handled"].get(key) != token and val:
                S["handled"][key] = token
                S["focus"] = (kind, val)
                st.rerun()
        st.caption(f"Click a row to open that {'dealer' if kind == 'dealer' else kind.upper()}'s dashboard")
    else:
        st.dataframe(d, hide_index=True, width="stretch", height=h, column_config=cfg)


def html_bars(spec):
    """RESP-style horizontal bar rows with a brass target marker (single-series hbar charts)."""
    name, ys = next(iter(spec.series.items()))
    pairs = [(lab, v) for lab, v in zip(spec.x, ys) if v is not None and v == v]   # no value -> no bar
    vals = [v for _, v in pairs]
    top = max(vals + ([spec.target] if spec.target else []) + [0]) or 1
    rows = []
    for lab, v in pairs[:40]:
        w = max(0, v) / top * 100
        lo = " lo" if spec.target is not None and v < spec.target else ""
        tm = f"<em style='left:{spec.target / top * 100:.1f}%'></em>" if spec.target is not None else ""
        txt = f"{v * 100:.1f}%" if spec.pct else f"{v:,.0f}"
        rows.append(f"<div class='brow'><span class='n' title='{esc(lab)}'>{esc(lab)}</span>"
                    f"<span class='t'><i class='{lo.strip()}' style='width:{w:.1f}%'></i>{tm}</span><span class='v num'>{txt}</span></div>")
    leg = ""
    if spec.target is not None:
        leg = (f"<div class='legend'><span><i style='background:var(--accent)'></i>At or above target</span>"
               f"<span><i style='background:var(--bad)'></i>Below target</span>"
               f"<span><i style='background:var(--brass);width:3px'></i>Target {spec.target * 100 if spec.pct else spec.target:.0f}{'%' if spec.pct else ''}</span></div>")
    return f"<div class='btitle'>{esc(spec.title)}</div><div class='bars'>{''.join(rows)}</div>{leg}"


def export_bar(rep: Report, key: str):
    sig = hashlib.md5(f"{key}|{rep.title}|{len(rep.blocks)}|{DATA_SIG}|{json.dumps(TARGETS, sort_keys=True)}".encode()).hexdigest()[:12]
    cols = st.columns([4.6, 1, 1, 1, 1, 1.35])
    cols[0].markdown("<div class='note' style='padding-top:8px'>Export this view</div>", unsafe_allow_html=True)
    cache = S["exports"]
    for c, fmt, lab in zip(cols[1:5], ("html", "pdf", "pptx", "csv"), ("HTML", "PDF", "PPT", "CSV")):
        ck = f"{sig}:{fmt}"
        if fmt in ("html", "csv") and ck not in cache:
            cache[ck] = export(rep, fmt)
        if ck in cache:
            data, fn, mime = cache[ck]
            c.download_button("⬇ " + lab, data, file_name=fn, mime=mime,
                              key=f"dl{ck}", width="stretch")
        elif c.button(lab, key=f"mk{ck}", width="stretch", help=f"Build the {lab} file"):
            with st.spinner(f"Building {fmt.upper()}…"):
                cache[ck] = export(rep, fmt)
            st.rerun()
    if cols[5].button("＋ Report pack", key=f"pin{sig}", width="stretch", help="Collect several views into one export"):
        S["pins"].append(rep); st.toast("Added to the report pack")


def render(rep: Report, key: str, title=True):
    if title:
        st.markdown(f"<div class='view-h'>{esc(rep.title)}</div>" + (f"<p class='lead'>{esc(rep.subtitle)}</p>" if rep.subtitle else ""),
                    unsafe_allow_html=True)
    export_bar(rep, key)
    for i, b in enumerate(rep.blocks):
        if b.kind == "heading":
            st.markdown(f"<div class='sec-h'>{esc(b.title)}</div>", unsafe_allow_html=True)
        elif b.kind == "text":
            st.markdown(b.text)
        elif b.kind == "bullets":
            items = "".join(f"<div>{_md(x)}</div>" for x in b.items)
            st.markdown((f"<div class='btitle'>{esc(b.title)}</div>" if b.title else "") + f"<div class='ins'>{items}</div>",
                        unsafe_allow_html=True)
        elif b.kind == "kpis":
            tiles = "".join(f"<div class='tile'><span class='l'>{esc(str(k[0]))}</span><span class='v num'>{esc(str(k[1]))}</span>"
                            f"<span class='s'>{esc(str(k[2])) if len(k) > 2 and k[2] else ''}</span></div>" for k in b.items)
            st.markdown((f"<div class='btitle'>{esc(b.title)}</div>" if b.title else "") + f"<div class='tiles'>{tiles}</div>",
                        unsafe_allow_html=True)
        elif b.kind == "table" and b.df is not None:
            show_table(b.df, f"{key}_{i}", b.title)
        elif b.kind == "chart" and b.chart is not None:
            if b.chart.kind == "hbar" and len(b.chart.series) == 1:
                st.markdown(html_bars(b.chart), unsafe_allow_html=True)
            else:
                st.plotly_chart(to_plotly(b.chart), width="stretch", key=f"ch{key}{i}", config={"displaylogo": False})


def _md(s):
    s = esc(str(s))
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)


def entity_report(kind: str, ident: str) -> Report:
    if kind == "dealer":
        return A.dealer_report(pack, ident)
    r = pack.get("retention")
    snap = pack.get("re_dealer_snapshot")
    books = list(dict.fromkeys((["ROYAL ENFIELD"] if not snap.empty and (snap.get(kind, pd.Series(dtype=str)) == ident).any() else [])
                               + sorted(set(r[r[kind] == ident]["oem"]) - {"ROYAL ENFIELD"})))
    rep = Report(f"{'RM' if kind == 'rm' else 'ZM'} review - {ident}", " · ".join(f"{k} as of {fmt_date(v)}" for k, v in pack.asof.items()))
    if not books:
        return rep.p(f"No data found for {ident}.")
    for b in books:
        sub = A.oem_report(pack, b, zm=ident if kind == "zm" else None, rm=ident if kind == "rm" else None)
        rep.h(sub.title); rep.extend(sub)
    return rep


# --------------------------------------------------------------------------- #
# top bar + zone scope + KPI strip
# --------------------------------------------------------------------------- #
asof = max(pack.asof.values()) if pack.asof else ""
re_t = TARGETS["ROYAL ENFIELD"]; pc_t = TARGETS["SKODA"]
all_zms = sorted(set(pack.get("retention")["zm"]) | set(pack.get("re_dealer_snapshot").get("zm", pd.Series(dtype=str))) - {""})
chips = [f"<span class='chip'>RE targets <b>FY {re_t['FY']:.0%}</b> · <b>OY {re_t['OY']:.0%}</b></span>",
         f"<span class='chip'>Car targets <b>FY {pc_t['FY']:.0%}</b> · <b>OY {pc_t['OY']:.0%}</b> · <b>Overall {pc_t['TOT']:.0%}</b></span>",
         f"<span class='chip'>Amber <b>{S['amber']} pts</b> · min base <b>{S['min_base']}</b></span>",
         f"<span class='chip'><b>{len(pack.sources)}</b> MIS files · {esc(S.get('last_sync') or fmt_date(asof))}</span>"]
tcol, zcol = st.columns([5, 1.1], vertical_alignment="center")
with tcol:
    st.markdown(f"<div class='top'><div class='brand'><div class='mark'>MIS</div><div><h1>MIS Retention Analyst</h1>"
                f"<p>All OEMs · EIBL · MIS as of {esc(fmt_date(asof))}</p></div></div><div class='chips'>{''.join(chips)}</div></div>",
                unsafe_allow_html=True)
with zcol:
    opts = ["All zones"] + all_zms
    S["zm"] = st.selectbox("Zone", opts, index=opts.index(S["zm"]) if S["zm"] in opts else 0, label_visibility="collapsed",
                           help="Scope every tab, report and export to one ZM")
ZM = None if S["zm"] == "All zones" else S["zm"]
BOOK = ra.dealer_book(pack, ZM)
if BOOK.empty:
    st.error("No retention data could be loaded from the MIS files.")
    st.markdown("<div class='dq'>" + "".join(f"<div class='{esc(n['severity'])}'><b>{esc(n['source'])}</b>{esc(n['note'])}</div>"
                for n in pack.notes if n["severity"] in ("high", "medium")) + "</div>", unsafe_allow_html=True)
    st.caption("Check the file names match the expected patterns, then use Sync latest MIS from Google Drive or re-upload.")
    with st.container(border=True):
        data_controls()
    if st.button("Clear cached data and reload"):
        st.cache_resource.clear()
        for f in os.listdir(DATA_DIR):
            if f.lower().endswith((".xlsx", ".xlsb", ".xlsm")) or f == "_manifest.json":
                try: os.remove(os.path.join(DATA_DIR, f))
                except OSError: pass
        st.rerun()
    st.stop()


def kpi_card(lbl, pct, ach, base, tgt, sub_extra=""):
    if pct != pct:  # NaN
        return ""
    bad = pct < tgt
    m = max(tgt, pct, 1e-9)
    gap = max(0, int(round(tgt * base - ach)))
    return (f"<div class='kpi'><div class='lbl'>{esc(lbl)}</div><div class='val num{' bad' if bad else ''}'>{pct * 100:.1f}%</div>"
            f"<div class='sub num'>{ach:,.0f} renewed of {base:,.0f} due{esc(sub_extra)}</div>"
            f"<div class='bar'><i style='width:{min(100, pct / m * 100):.1f}%'></i><em style='left:{min(100, tgt / m * 100):.1f}%'></em></div>"
            f"<div class='foot num'><span>Target {tgt:.0%}</span><span>Gap <b>{gap:,}</b> policies</span></div></div>")


cards = []
for o in OEMS:
    b = BOOK[BOOK["oem"] == o] if not BOOK.empty else BOOK
    if b.empty: continue
    base, ach = b["Base"].sum(), b["Renewed"].sum()
    extra = ""
    if o == "ROYAL ENFIELD":
        rg = pack.get("re_region")
        if ZM and not rg.empty: rg = rg[[re_zm(x) == ZM for x in rg["region"]]]
        if not rg.empty: extra = f" · last month same day {rg['fy_ach_prev'].sum() / max(1, rg['fy_base_prev'].sum()):.1%}"
    cards.append(kpi_card(f"{OEM_LABEL[o]} · {b['Period'].iloc[0]}", ach / base if base else float("nan"), ach, base,
                          b["Target %"].iloc[0], extra))
st.markdown(f"<div class='kpis'>{''.join(cards)}</div>", unsafe_allow_html=True)

# --------------------------------------------------------------------------- #
# main panel + chat
# --------------------------------------------------------------------------- #
left, right = st.columns([1, 0.37], gap="medium")
SC = f"z{ZM or 'all'}"      # widget-key suffix per zone scope

with left:
    with st.container(key="mainpanel"):
        # fixed slot above the tabs: opening/closing a dashboard never shifts the tabs, so the selected tab is kept
        focus_slot = st.container(key="focusslot")
        TABS = ["Overview", "Dealers", "RMs", "RM dashboard", "Dealer dashboard", "OEM dashboards", "First year",
                "Royal Enfield", "Volvo", "Open cases", "Penetration", "Payout", "Reports", "Data & settings"]
        tabs = dict(zip(TABS, st.tabs(TABS)))

        with tabs["Overview"]:
            if ZM:
                render(ra.zone_exec(pack, ZM), f"ov{SC}")
            else:
                ov = A.overview_report(pack)
                ov.h("What needs action"); ov.bullets(ra.action_insights(BOOK, pack))
                render(ov, "ov")

        with tabs["Dealers"]:
            c1, c2, c3, c4 = st.columns([2, 1, 1.2, 1.3])
            q = c1.text_input("Search", placeholder="Dealer name or code", label_visibility="collapsed")
            fo = c2.selectbox("OEM", ["All OEMs"] + [OEM_LABEL[o] for o in OEMS], label_visibility="collapsed")
            frm = c3.selectbox("RM", ["All RMs"] + sorted(set(BOOK["RM"]) - {""}), label_visibility="collapsed")
            frag = c4.multiselect("RAG", ["Red", "Amber", "Green"], placeholder="All RAG", label_visibility="collapsed")
            oem_key = next((o for o in OEMS if OEM_LABEL[o] == fo), None)
            render(ra.dealer_list(pack, ZM, oem_key, frag or None, None if frm == "All RMs" else frm, q or None), f"dl{SC}")

        with tabs["RMs"]:
            render(ra.rm_scorecards(pack, ZM), f"rms{SC}")

        with tabs["RM dashboard"]:
            rms_all = sorted(set(BOOK["RM"]) - {""})
            pick = st.selectbox("RM", rms_all, index=None, placeholder="Choose an RM", key=f"rmdash{SC}")
            if pick:
                render(entity_report("rm", pick), f"rmd{pick}")
            else:
                st.markdown("<p class='lead'>Choose an RM for the full dashboard: every OEM in their book, trend, zones, "
                            "dealers, peers, penetration, open cases and payouts.</p>", unsafe_allow_html=True)

        with tabs["Dealer dashboard"]:
            dl = {x.Code: f"{x.Dealer} · {x.Code} · {x.OEM} · {str(x.RM).title()}" for x in BOOK.itertuples()}
            picks = st.multiselect("Dealers in scope", list(dl), format_func=lambda x: dl.get(x, x), key=f"dd{SC}",
                                   placeholder="Pick one dealer for its dashboard, or several for a combined report")
            if len(picks) == 1:
                c1, c2 = st.columns([1, 1])
                mode = c1.radio("View", ["Dealer dashboard", "Dealer meeting deck"], horizontal=True, label_visibility="collapsed")
                render(A.dealer_report(pack, picks[0]) if mode == "Dealer dashboard" else ra.meeting_deck(pack, picks[0]),
                       f"dd1{picks[0]}{mode[:3]}")
            elif len(picks) > 1:
                render(ra.selection_report(pack, picks), f"ddn{'-'.join(picks)[:60]}")
            else:
                st.markdown("<p class='lead'>Tick one dealer for its dashboard or the dealer-facing meeting deck (anonymised "
                            "peers, no RM visit data, ends on agreed actions); tick several for each code in full plus a "
                            "side-by-side summary. Every view exports.</p>", unsafe_allow_html=True)

        with tabs["OEM dashboards"]:
            r = pack.get("retention")
            c1, c2 = st.columns(2)
            oem = c1.selectbox("OEM", OEMS + ["ALL"], format_func=lambda x: OEM_LABEL.get(x, "Private car - all brands"), key="oemsel")
            if oem == "ROYAL ENFIELD":
                snap = pack.get("re_dealer_snapshot")
                rms = sorted(set(snap[snap["zm"] == ZM]["rm"] if ZM else snap.get("rm", pd.Series(dtype=str))) - {""})
            else:
                d = r[r["oem"] == oem] if oem != "ALL" else r[r["book"] == "Private car"]
                rms = sorted(set(d[d["zm"] == ZM]["rm"] if ZM else d["rm"]) - {""})
            rm = c2.selectbox("RM", ["All"] + rms, key=f"oemrm{oem}{SC}")
            render(A.oem_report(pack, oem, ZM, None if rm == "All" else rm), f"oem{oem}{rm}{SC}")

        with tabs["First year"]:
            render(ra.fy_vs_oy(pack, ZM), f"fy{SC}")
        with tabs["Royal Enfield"]:
            render(A.re_report(pack, ZM), f"re{SC}")
        with tabs["Volvo"]:
            render(ra.volvo_page(pack, ZM), f"vo{SC}")
        with tabs["Open cases"]:
            render(ra.build(pack, "open", ZM), f"oc{SC}")
        with tabs["Penetration"]:
            render(ra.penetration_report(pack, ZM), f"pen{SC}")
        with tabs["Payout"]:
            render(ra.payout_report(pack, ZM), f"pay{SC}")

        with tabs["Reports"]:
            st.markdown("<p class='lead'>Pick a report and its scope, then build it. Every report renders here and exports "
                        "to HTML, PDF, PowerPoint or CSV. For anything else, ask the analyst - Claude composes it on demand.</p>",
                        unsafe_allow_html=True)
            labels = {rid: lab for rid, lab, _ in ra.REPORTS}
            c1, c2, c3 = st.columns([1.4, 1, 1.4])
            rid = c1.selectbox("Report", list(labels), format_func=lambda x: labels[x], key="rpt")
            rzm = c2.selectbox("Zone", ["All zones"] + all_zms, index=(all_zms.index(ZM) + 1) if ZM else 0, key=f"rptzm{SC}")
            rzm = None if rzm == "All zones" else rzm
            who = None
            if rid in ("meeting", "dealer"):
                dl2 = {x.Code: f"{x.Dealer} · {x.Code} · {x.OEM}" for x in ra.dealer_book(pack, rzm).itertuples()}
                who = c3.selectbox("Dealer", list(dl2), format_func=lambda x: dl2.get(x, x), index=None, placeholder="Choose dealer", key="rptd")
            elif rid == "rm":
                who = c3.selectbox("RM", sorted(set(ra.dealer_book(pack, rzm)["RM"]) - {""}), index=None, placeholder="Choose RM", key="rptr")
            st.caption(next(d for i, _, d in ra.REPORTS if i == rid))
            if st.button("Build report", type="primary", key="rptgo"):
                S["built"] = (rid, rzm, who)
            if S.get("built"):
                b_rid, b_zm, b_who = S["built"]
                if b_rid == "rm":
                    rep = entity_report("rm", b_who) if b_who else Report("Full RM review").p("Choose an RM.")
                elif b_rid == "dealer":
                    rep = A.dealer_report(pack, b_who) if b_who else Report("Full dealer review").p("Choose a dealer.")
                else:
                    rep = ra.build(pack, b_rid, b_zm, b_who)
                render(rep, f"rpt{b_rid}{b_zm}{b_who}")
            st.markdown("<div class='sec-h'>Report pack</div>", unsafe_allow_html=True)
            if not S["pins"]:
                st.markdown("<p class='lead'>Use <b>＋ Report pack</b> on any view to collect sections, then export them "
                            "here as one file.</p>", unsafe_allow_html=True)
            else:
                combo = Report("MIS report pack", " · ".join(f"{k} as of {fmt_date(v)}" for k, v in pack.asof.items()))
                for p_ in S["pins"]:
                    combo.h(p_.title); combo.extend(p_)
                st.markdown("<div class='ins'>" + "".join(f"<div>{i + 1}. {esc(p_.title)}</div>" for i, p_ in enumerate(S["pins"])) + "</div>",
                            unsafe_allow_html=True)
                if st.button("Clear pack"):
                    S["pins"] = []; st.rerun()
                render(combo, "combo")

        with tabs["Data & settings"]:
            data_controls()
            st.markdown("<div class='sec-h'>Claude analyst</div>", unsafe_allow_html=True)
            provs = ["gemini", "groq", "openrouter", "openai", "claude"]
            plabel = {"gemini": "Google Gemini (free key)", "groq": "Groq (free key)", "openrouter": "OpenRouter (free models)",
                      "openai": "OpenAI (paid)", "claude": "Anthropic Claude (paid)"}
            c1, c2 = st.columns([1.3, 2])
            S["provider"] = c1.selectbox("Provider", provs, index=provs.index(S["provider"]) if S["provider"] in provs else 0,
                                         format_func=lambda x: plabel[x])
            k = c2.text_input("API key", type="password", value="", placeholder="using the key from app secrets (or paste one here)")
            if k: S["api_key"] = k
            if S["provider"] == "claude":
                c1, c2 = st.columns(2)
                models = [DEFAULT_MODEL, "claude-sonnet-5-5", "claude-haiku-4-5"]
                S["model"] = c1.selectbox("Model", models, index=models.index(S["model"]) if S["model"] in models else 0)
                S["effort"] = c2.selectbox("Effort", EFFORTS, index=EFFORTS.index(S["effort"]) if S["effort"] in EFFORTS else 2)
            else:
                st.caption("Free key: aistudio.google.com/apikey (Gemini) or console.groq.com/keys (Groq). Model: "
                           + PROVIDERS[S["provider"]]["model"])
            st.markdown("<div class='sec-h'>Criteria - what counts as critical</div>", unsafe_allow_html=True)
            c1, c2 = st.columns(2)
            S["amber"] = c1.number_input("Amber band (points below target)", 0, 50, int(S["amber"]))
            S["min_base"] = c2.number_input("Minimum base to flag a dealer", 0, 500, int(S["min_base"]))
            tgt = S["targets"]
            for o in OEMS:
                c0, c1, c2, c3 = st.columns([1.3, 1, 1, 1])
                c0.markdown(f"<div style='padding-top:30px;font-weight:600'>{OEM_LABEL[o]}</div>", unsafe_allow_html=True)
                tgt[o]["FY"] = c1.number_input("First year %", 0, 100, int(round(tgt[o]["FY"] * 100)), key=f"t{o}fy") / 100
                tgt[o]["OY"] = c2.number_input("Other year %", 0, 100, int(round(tgt[o]["OY"] * 100)), key=f"t{o}oy") / 100
                tgt[o]["TOT"] = c3.number_input("Overall %", 0, 100, int(round(tgt[o]["TOT"] * 100)), key=f"t{o}tot") / 100
            st.caption("RE first-year 30% and Volvo 75% come from the MIS files; Audi / Skoda / VW targets are placeholders - set yours. "
                       "RAG, policies-to-target, flags and insights everywhere follow these criteria.")
            st.markdown("<div class='sec-h'>Data quality</div>", unsafe_allow_html=True)
            st.markdown("<div class='dq'>" + "".join(
                f"<div class='{esc(n['severity'])}'><b>{esc(n['source'])}</b>{esc(n['note'])}</div>" for n in pack.notes) + "</div>",
                unsafe_allow_html=True)
            render(ra.data_corrections(pack), "dq")
            with st.expander("Files expected"):
                st.dataframe(pd.DataFrame([{"Type": v[1], "Book": v[2], "File name pattern": v[0]} for v in FILE_TYPES.values()]),
                             hide_index=True, width="stretch")

    with focus_slot:
        foc = S.get("focus")
        if foc and S.get("focus_shown") != foc:
            S["focus_shown"] = foc      # newly opened -> bring it into view
            import streamlit.components.v1 as components
            components.html("<script>const d=window.parent.document;const m=d.querySelector('[data-testid=stMain]')||d.scrollingElement;"
                            "(m.scrollTo?m:window.parent).scrollTo({top:0,behavior:'smooth'});"
                            "(d.scrollingElement||d.body).scrollTo({top:0,behavior:'smooth'});</script>", height=0)
        if foc:
            kind, ident = foc
            c1, c2 = st.columns([6, 1])
            label = {"dealer": "Dealer dashboard", "rm": "RM dashboard", "zm": "ZM dashboard", "answer": "Analyst answer"}[kind]
            c1.markdown(f"<div class='focus'><span class='k'>{label}</span></div>", unsafe_allow_html=True)
            if c2.button("✕ Close", key="close_focus", width="stretch"):
                S["focus"] = None; st.rerun()
            if kind == "answer":
                i = int(ident)
                if 0 <= i < len(S["chat"]) and S["chat"][i].get("report") is not None:
                    render(S["chat"][i]["report"], f"fa{i}")
            else:
                render(entity_report(kind, ident), f"f{kind}{ident}")
            st.divider()

with right:
    with st.container(key="chatpanel"):
        on = bool(API_KEY)
        st.markdown(f"<div class='chead'><span class='dot{'' if on else ' off'}'></span><div><h2>Ask the analyst</h2>"
                    f"<p>{(('Claude · ' + esc(S['model']) + ' · effort ' + esc(S['effort'])) if S['provider'] == 'claude' else (esc(S['provider'].title()) + ' · ' + esc(PROVIDERS[S['provider']]['model']))) if on else 'Offline analyst - add a free Gemini key in Data & settings'}"
                    f"{' · ' + esc(ZM.title()) if ZM else ''}</p></div></div>", unsafe_allow_html=True)
        box = st.container(height=620, border=False)
        with box:
            if not S["chat"]:
                st.markdown("<p class='note'>Ask about any OEM, zone, RM or dealer - rankings, trends, gaps to target, "
                            "why a number moved, what to do next, or any report you need. Claude runs queries on the live "
                            "MIS data, builds tables and charts, and every answer can be exported.</p>", unsafe_allow_html=True)
            for i, m in enumerate(S["chat"]):
                with st.chat_message(m["role"], avatar="🧑" if m["role"] == "user" else "📊"):
                    st.markdown(m["content"])
                    if m.get("report") is not None:
                        b1, b2, b3 = st.columns(3)
                        if b1.button("Open full answer", key=f"open{i}", width="stretch"):
                            S["focus"] = ("answer", str(i)); st.rerun()
                        for col, fmt, lab in ((b2, "pdf", "PDF"), (b3, "pptx", "PPT")):
                            ck = f"ans{i}:{fmt}"
                            if ck in S["exports"]:
                                data, fn, mime = S["exports"][ck]
                                col.download_button("⬇ " + lab, data, file_name=fn, mime=mime, key=f"adl{i}{fmt}", width="stretch")
                            elif col.button(lab, key=f"a{fmt}{i}", width="stretch"):
                                S["exports"][ck] = export(m["report"], fmt); st.rerun()
                        if m.get("steps"):
                            st.caption("Tools: " + " → ".join(dict.fromkeys(s.split("(")[0].split(" ")[0] for s in m["steps"])))
                        if m.get("error"):
                            st.caption(f"⚠️ {m['error']}")
        q = st.chat_input("Ask about any OEM, zone, RM or dealer…")
        if q:
            with box:
                with st.chat_message("user", avatar="🧑"):
                    st.markdown(q)
                with st.chat_message("assistant", avatar="📊"):
                    with st.status("Analysing…", expanded=False) as stt:
                        ask = q if not ZM else f"{q}\n\n(Scope: zone {ZM} unless the question says otherwise.)"
                        ans = run_agent(pack, ask, [{"role": m["role"], "content": m["content"]} for m in S["chat"]],
                                        api_key=API_KEY, provider=S["provider"],
                                        model=S["model"] if S["provider"] == "claude" else "", effort=S["effort"],
                                        on_step=lambda n, a: stt.update(label=f"Running {n}…"))
                        stt.update(label="Done", state="complete")
            S["chat"] += [{"role": "user", "content": q},
                          {"role": "assistant", "content": ans.text, "report": ans.report, "steps": ans.steps, "error": ans.error}]
            S["focus"] = ("answer", str(len(S["chat"]) - 1))
            st.rerun()
        if S["chat"] and st.button("Clear conversation", key="clearchat"):
            S["chat"] = []; S["focus"] = None; st.rerun()
