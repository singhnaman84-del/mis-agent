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
from mis import views as V
from mis import mgmt as M
from mis.agent import DEFAULT_MODEL, EFFORTS, PROVIDERS, run_agent
from mis.charts import BRASS, to_plotly
from mis.loaders import FILE_TYPES, load_folder
from mis.model import CATALOG, OEM_LABEL, OEMS, TARGETS, re_zm
from mis.report import PCT_HINT, Report, export, fmt_cell

# Force the light RESP theme from code, so every device (Windows, Android, iOS, dark mode or not) gets the same look
# even if .streamlit/config.toml is missing from the deployment.
for _k, _v in {"theme.base": "light", "theme.primaryColor": "#1F5B3F", "theme.backgroundColor": "#EEF1F4",
               "theme.secondaryBackgroundColor": "#F6F8F9", "theme.textColor": "#14202B"}.items():
    try:
        st._config.set_option(_k, _v)
    except Exception:
        pass
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
.kpi .per{font-size:11.5px;color:var(--ink-3);margin-top:-4px}
.kpi .val.bad, .stApp .kpi .val.bad{color:var(--bad)} .kpi .val.warn, .stApp .kpi .val.warn{color:var(--warn)}
.kpi .val.good, .stApp .kpi .val.good{color:var(--good)}
.kpi .bar i.bad{background:var(--bad)} .kpi .bar i.warn{background:#C9962B}
.kpi .pl .pen{border-left:1px solid var(--line);padding-left:10px;color:var(--accent)}
.scopebar{font-family:var(--font-d);font-size:12px;letter-spacing:1px;text-transform:uppercase;color:var(--ink-3);margin:10px 2px -4px}
.msgs{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:8px;margin:6px 0 10px}
.msgs div{background:var(--surface-2);border-left:3px solid var(--brass);border-radius:6px;padding:9px 11px;font-size:13px;color:var(--ink)}
.mlist{border:1px solid var(--line-soft);border-radius:8px;padding:8px 10px;margin-bottom:10px}
.mlist .mh{font-family:var(--font-d);font-size:11px;letter-spacing:1px;text-transform:uppercase;color:var(--ink-3);margin-bottom:4px}
.ml{display:grid;grid-template-columns:1fr 58px 62px;gap:8px;font-size:13px;padding:4px 0;border-top:1px solid var(--line-soft)}
.ml:first-of-type{border-top:0} .ml .n{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--ink)}
.ml .v{text-align:right;font-weight:600} .ml .v.bad{color:var(--bad)} .ml .v.good{color:var(--good)} .ml .g{text-align:right;color:var(--ink-3)}
.kpi .pl{display:flex;gap:12px;flex-wrap:wrap;font-size:12.5px;color:var(--ink-2)}
.kpi .pl b{color:var(--ink);font-weight:600} .kpi .pl b.bad{color:var(--bad)}
.stApp .kpi .pl b.bad{color:var(--bad)}
/* top-bar controls sit level with the brand strip */
.top{margin-bottom:0 !important}
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
/* phone + dark-mode safety: keep the RESP light look even if the device is in dark mode or the theme file is missing */
:root{color-scheme:only light}   /* "only" also opts out of Android Chrome / Samsung Internet forced dark mode */
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}   /* iOS: no text inflation in landscape */
.block-container{padding-left:max(16px, env(safe-area-inset-left)) !important;padding-right:max(16px, env(safe-area-inset-right)) !important}
/* tablets and small laptops: stack the chat under the main panel instead of squeezing both */
@media (max-width: 1100px){
  [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] .st-key-mainpanel){flex-direction:column !important;gap:12px !important}
  [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] .st-key-mainpanel) > [data-testid="stColumn"]{width:100% !important;flex:1 1 100% !important;min-width:0 !important}
  .st-key-chatpanel{position:static !important}
}
/* touch screens: bigger tap targets */
@media (pointer: coarse){
  .stButton button, .stDownloadButton button{min-height:40px !important}
  [data-baseweb="tab"]{min-height:40px}
}
.stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"], [data-testid="stMainBlockContainer"]{background:var(--ground) !important;color:var(--ink) !important}
.stApp p, .stApp li, .stApp label, .stApp span, .stApp h1, .stApp h2, .stApp h3, .stApp h4{color:var(--ink)}
.stApp [data-baseweb="select"] > div, .stApp input, .stApp textarea{background:#fff !important;color:var(--ink) !important}
[data-baseweb="popover"] *, [data-baseweb="menu"] *{background-color:#fff;color:var(--ink)}
.stApp .num, .stApp .kpi .val{color:var(--ink)} .stApp .kpi .val.bad{color:var(--bad)}
@media (max-width: 768px){
  .block-container{padding:0 8px 24px !important}
  /* iOS Safari zooms into any field under 16px when it is tapped - keep inputs at 16px */
  .stApp input, .stApp textarea, .stApp [data-baseweb="select"] div{font-size:16px !important}
  [data-testid="stChatInput"] textarea{font-size:16px !important}
  /* top bar: brand full width, OEM + zone side by side */
  [data-testid="stHorizontalBlock"]:has(.st-key-oemsel){flex-direction:row !important;flex-wrap:wrap !important;gap:8px !important}
  [data-testid="stHorizontalBlock"]:has(.st-key-oemsel) > [data-testid="stColumn"]{min-width:0 !important;flex:1 1 40% !important;width:auto !important}
  [data-testid="stHorizontalBlock"]:has(.st-key-oemsel) > [data-testid="stColumn"]:first-child{flex-basis:100% !important}
  .kpi .val{font-size:26px}
  [role="tablist"]{flex-wrap:wrap !important;overflow:visible !important;gap:4px !important;border-bottom:0 !important;height:auto !important}
  [role="tab"]{padding:5px 11px !important;border:1px solid var(--line) !important;border-radius:16px !important;background:var(--surface) !important;height:auto !important}
  [role="tab"][aria-selected="true"]{background:var(--accent) !important}
  [role="tab"][aria-selected="true"] p{color:#fff !important}
  [role="tab"] p{font-size:13px}
  [data-baseweb="tab-highlight"], [data-baseweb="tab-border"]{display:none !important}
  [data-testid="stTabs"] button[aria-label*="croll"], [data-testid="stTabs"] > div > div:has(> [role="tablist"]) ~ button{display:none !important}
  .kpis{grid-template-columns:1fr 1fr;gap:8px} .kpi{padding:10px 12px} .kpi .val{font-size:26px} .kpi .sub{font-size:11.5px}
  .tiles{grid-template-columns:1fr 1fr} .brow{grid-template-columns:100px 1fr 62px}
  .st-key-chatpanel{position:static} .chips{margin-left:0} .top{padding:10px 12px}
  /* export bar: one wrapping row of small buttons instead of five full-width rows */
  [class*="st-key-xbar"] [data-testid="stHorizontalBlock"]{flex-direction:row !important;flex-wrap:wrap !important;gap:6px !important}
  [class*="st-key-xbar"] [data-testid="stColumn"]{width:auto !important;flex:1 1 auto !important;min-width:0 !important}
  [class*="st-key-xbar"] [data-testid="stColumn"]:first-child{display:none !important}
}
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
    from mis.loaders import load_folder_cached
    return load_folder_cached(folder, sig)


@st.cache_data(ttl=1800, show_spinner=False)
def remote_sig(bucket):
    """What Drive holds right now (one listing call, cached 30 min across all viewers)."""
    from mis.drive import remote_signature
    return remote_signature(sa_info(), secret("DRIVE_FOLDER_ID"))


def sa_info():
    try:
        return dict(st.secrets["gcp_service_account"])
    except Exception:
        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        return json.loads(raw) if raw else None


def drive_sync(quiet=False):
    from mis.drive import sync
    bar = st.progress(0.0, "New data in Google Drive - updating…" if quiet else "Connecting to Google Drive…")
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

# auto-refresh: when Drive holds newer files than the local copy, pull them (checked at most every 30 minutes)
if sa_info():
    try:
        _rs = remote_sig(int(time.time() // 1800))
        _lp = os.path.join(DATA_DIR, "_remote_sig.txt")
        _local = open(_lp).read().strip() if os.path.exists(_lp) else ""
        if _rs and _rs != _local and S.get("_synced_for") != _rs:
            S["_synced_for"] = _rs
            drive_sync(quiet=True)
    except Exception as _e:
        S["_autosync_error"] = f"{type(_e).__name__}: {_e}"
DATA_SIG = folder_sig(DATA_DIR)
with st.spinner("Reading MIS workbooks… (first load ~30-60 s, then cached)"):
    pack = get_pack(DATA_DIR, DATA_SIG)

# settings live in session state (targets, Claude)
S.setdefault("targets", json.loads(secret("TARGETS_JSON", "null") or "null") or json.loads(json.dumps(TARGETS)))
for o in OEMS:
    TARGETS[o].update(S["targets"][o])
S.setdefault("amber", 10); S.setdefault("min_base", 10); S.setdefault("zm", "All zones")
A.set_criteria(S["amber"] / 100, S["min_base"])
def provider_of_key(k: str) -> str | None:
    """Recognise the provider from the key itself, so a Groq key never gets sent to Gemini (or vice versa)."""
    k = (k or "").strip()
    if k.startswith("gsk_"): return "groq"
    if k.startswith("xai-"): return "xai"
    if k.startswith("AIza"): return "gemini"
    if k.startswith("sk-or-"): return "openrouter"
    if k.startswith("sk-ant-"): return "claude"
    if k.startswith("sk-"): return "openai"
    return None


_cfg_key = str(secret("LLM_API_KEY", "") or "").strip()
_cfg_provider = provider_of_key(_cfg_key) or str(secret("LLM_PROVIDER", "") or "").strip().lower() or \
    next((p for p in ("groq", "xai", "gemini", "openrouter", "openai") if secret(f"{p.upper()}_API_KEY")), None) or \
    ("claude" if secret("ANTHROPIC_API_KEY") else "gemini")
# the secrets win over an older browser session: if they changed since this session started, follow them
if S.get("_cfg_provider") != _cfg_provider:
    S["_cfg_provider"] = S["provider"] = _cfg_provider
S.setdefault("provider", _cfg_provider)
S.setdefault("model", secret("CLAUDE_MODEL", DEFAULT_MODEL))
S.setdefault("effort", secret("CLAUDE_EFFORT", "high"))
if S.get("api_key") and provider_of_key(S["api_key"]) and provider_of_key(S["api_key"]) != S["provider"]:
    S["provider"] = provider_of_key(S["api_key"])          # a pasted key decides the provider too
_key_for = {"claude": secret("ANTHROPIC_API_KEY")}
API_KEY = (S.get("api_key") or _key_for.get(S["provider"]) or secret(f"{S['provider'].upper()}_API_KEY")
           or (_cfg_key if provider_of_key(_cfg_key) in (None, S["provider"]) else "") or "").strip()
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
    with st.container(key=f"xbar{sig}"):
        _export_buttons(rep, sig)


def _export_buttons(rep: Report, sig: str):
    """One click per format: PDF / PPT are built at the moment of the click (no second 'download' button)."""
    cols = st.columns([4.6, 1, 1, 1, 1, 1.35])
    cols[0].markdown("<div class='note' style='padding-top:8px'>Export this view</div>", unsafe_allow_html=True)
    cache = S["exports"]
    base = f"{rep.slug()}_{dt.date.today():%Y-%m-%d}"
    mimes = {"html": "text/html", "pdf": "application/pdf",
             "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}
    for c, fmt, lab in zip(cols[1:5], ("html", "pdf", "pptx", "csv"), ("HTML", "PDF", "PPT", "CSV")):
        ck = f"{sig}:{fmt}"
        if fmt in ("html", "csv"):
            if ck not in cache:
                cache[ck] = export(rep, fmt)
            data, fn, mime = cache[ck]
        else:
            data = (lambda r=rep, f=fmt: export(r, f)[0])        # built on click, on a background thread
            fn, mime = f"{base}.{fmt}", mimes[fmt]
        c.download_button("⬇ " + lab, data, file_name=fn, mime=mime, key=f"dl{ck}", width="stretch",
                          help=f"Download as {lab}")
    if cols[5].button("＋ Report pack", key=f"pin{sig}", width="stretch", help="Collect several views into one export"):
        S["pins"].append(rep); st.toast("Added to the report pack")


def render(rep: Report, key: str, title=True, skip_kpis=False):
    """skip_kpis: hide the report's first KPI block on screen (the page's KPI strip already shows it; exports keep it)."""
    if title:
        st.markdown(f"<div class='view-h'>{esc(rep.title)}</div>" + (f"<p class='lead'>{esc(rep.subtitle)}</p>" if rep.subtitle else ""),
                    unsafe_allow_html=True)
    export_bar(rep, key)
    first_kpi = next((i for i, b in enumerate(rep.blocks) if b.kind == "kpis"), None) if skip_kpis else None
    for i, b in enumerate(rep.blocks):
        if i == first_kpi:
            continue
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
                st.plotly_chart(to_plotly(b.chart), width="stretch", key=f"ch{key}{i}", config={"displaylogo": False, "displayModeBar": False, "responsive": True})


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
# what the Drive folder actually holds - pages, tabs and sections are built from this
def _has(t, oem=None):
    d = pack.get(t)
    if d.empty: return False
    return oem is None or "oem" not in d or (d["oem"] == oem).any()


AVAIL = {}
for _o in V.PAGES[1:]:
    _ret = _has("re_dealer_snapshot") or _has("retention", _o) if _o == "ROYAL ENFIELD" else _has("retention", _o)
    AVAIL[_o] = {"retention": _ret,
                 "penetration": (_has("re_penetration") if _o == "ROYAL ENFIELD" else _has("volvo_safenet") if _o == "VOLVO"
                                 else _has("pc_penetration_month", _o)),
                 "payout": _has("re_payouts") if _o == "ROYAL ENFIELD" else _has("pc_payouts", _o),
                 "first_year": _o == "ROYAL ENFIELD" and _has("re_region"),
                 "open_cases": _o == "VOLVO" and _has("volvo_open_cases")}
PAGES = list(V.PAGES)          # fixed layout: every OEM page always exists; contents follow the files in Drive
NEED = {"retention": {"ROYAL ENFIELD": "RESP Retention Report (Daily Mail) or the RE Dealer Performance Incentive Tracker",
                      "VOLVO": "VOLVO Daily Renewal Retention / VOLVO Retention Summary",
                      "_": "YTD-MTD Dealer And RM & ZM Wise Renewal Report"},
        "penetration": {"ROYAL ENFIELD": "Penetration Report as on <date> (Royal Enfield)", "_": "Penetration report <Mon'YY> / YTD SAVWIPL Penetration MIS"},
        "payout": {"ROYAL ENFIELD": "RE_UTR DETAILS", "_": "SAVWIPL & VC_UTR DETAILS"},
        "first_year": {"_": "RESP Renewal Report As On <date>"},
        "open_cases": {"_": "Volvo Conversion ... Not Renewed Cases"}}


def available(what):
    if PAGE == "OVERALL":
        return any(a.get(what) for a in AVAIL.values())
    return AVAIL.get(PAGE, {}).get(what, False)


def missing(what):
    """Same tab, same place - just says which Drive file would fill it."""
    f = NEED[what].get(PAGE, NEED[what]["_"])
    st.markdown(f"<div class='msgs'><div><b>No {esc(what.replace('_', ' '))} data for {esc(V.PAGE_LABEL[PAGE])} in the current "
                f"Google Drive files.</b><br>This view fills itself as soon as the folder holds: <i>{esc(f)}</i>. "
                "The app checks Drive every 30 minutes.</div></div>", unsafe_allow_html=True)


def file_tab_name(path):
    """Tab label from the file name: dates, numbers and the extension removed, short."""
    n = os.path.splitext(os.path.basename(path))[0]
    n = re.sub(r"\b\d{1,2}[-_ .]?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-_ .']?\d{0,4}\b", " ", n, flags=re.I)
    n = re.sub(r"\b(as on|as of|till|upto|up to)\b.*$", " ", n, flags=re.I)
    n = re.sub(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*'?\d{2,4}\b", " ", n, flags=re.I)
    n = re.sub(r"[_]+|\b\d[\d\-./]*\b", " ", n)
    n = re.sub(r"\s+", " ", n).strip(" -.") or os.path.basename(path)
    return n if len(n) <= 28 else n[:26].rstrip() + "…"


FILE_TABS = {}
for _p in getattr(pack, "others", []):
    _n = file_tab_name(_p)
    while _n in FILE_TABS: _n += " ·"
    FILE_TABS[_n] = _p

# --------------------------------------------------------------------------- #
# top bar: OEM -> ZM -> RM drill + assistant toggle
# --------------------------------------------------------------------------- #
asof = max(pack.asof.values()) if pack.asof else ""
S.setdefault("page", "OVERALL"); S.setdefault("show_chat", True); S.setdefault("rm", "All RMs")
tcol, ocol, zcol, rcol, ccol = st.columns([2.6, 1.15, 1.25, 1.25, 0.85], vertical_alignment="center")
with tcol:
    st.markdown(f"<div class='top'><div class='brand'><div class='mark'>MIS</div><div><h1>MIS Retention Analyst</h1>"
                f"<p>EIBL · MIS as of {esc(fmt_date(asof))} · {len(pack.sources)} files</p></div></div></div>",
                unsafe_allow_html=True)
with ocol, st.container(key="oemsel"):
    if S.get("_goto_page") in PAGES:
        S["oemsel_w"] = S.pop("_goto_page")
    if S.get("oemsel_w") not in PAGES: S["oemsel_w"] = S["page"] if S["page"] in PAGES else "OVERALL"
    S["page"] = st.selectbox("OEM", PAGES, key="oemsel_w",
                             format_func=lambda x: V.PAGE_LABEL[x], label_visibility="collapsed",
                             help="Overall shows every OEM; each OEM opens its own dashboards")
PAGE = S["page"]


def zms_for(page):
    r = pack.get("retention")
    snap = pack.get("re_dealer_snapshot")
    re_z = set(snap.get("zm", pd.Series(dtype=str)))
    if page == "OVERALL": z = set(r["zm"]) | re_z
    elif page == "ROYAL ENFIELD": z = re_z
    else: z = set(r[r["oem"] == page]["zm"])
    return sorted(z - {""})


def rms_for(page, zm):
    r = pack.get("retention")
    snap = pack.get("re_dealer_snapshot")
    out = set()
    if page in ("OVERALL", "ROYAL ENFIELD") and not snap.empty:
        out |= set(snap[snap["zm"] == zm]["rm"] if zm else snap["rm"])
    if page != "ROYAL ENFIELD":
        d = r[r["book"] == "Private car"] if page == "OVERALL" else r[r["oem"] == page]
        out |= set(d[d["zm"] == zm]["rm"] if zm else d["rm"])
    return sorted(out - {""})


all_zms = zms_for("OVERALL")
page_zms = zms_for(PAGE)
with zcol:
    if S.get("_goto_zm"):
        S["zm_w"] = S.pop("_goto_zm")
    opts = ["All zones"] + page_zms
    if S.get("zm_w") not in opts: S["zm_w"] = "All zones"
    S["zm"] = st.selectbox("Zone", opts, key="zm_w", label_visibility="collapsed", help="Zonal manager - scopes every tab")
ZM = None if S["zm"] == "All zones" else S["zm"]
with rcol:
    rm_opts = ["All RMs"] + (rms_for(PAGE, ZM) if ZM else [])
    if S.get("_goto_rm"):
        S["rm_w"] = S.pop("_goto_rm")
    if S.get("rm_w") not in rm_opts: S["rm_w"] = "All RMs"
    S["rm"] = st.selectbox("RM", rm_opts, key="rm_w", label_visibility="collapsed", disabled=not ZM,
                           help="Pick a zone first - then any RM in it")
RM = None if S["rm"] == "All RMs" or not ZM else S["rm"]
with ccol:
    S["show_chat"] = st.toggle("Assistant", value=S["show_chat"], help="Show or hide the chat panel")
BOOK = ra.dealer_book(pack, ZM)
NO_RETENTION = pack.get("retention").empty and pack.get("re_dealer_snapshot").empty
if NO_RETENTION:
    # same layout as always - a slim banner says what is missing; the tabs show where each file would go
    st.markdown("<div class='msgs'><div><b>No retention report in the current Google Drive files.</b> The dashboards fill "
                "themselves as soon as a renewal / retention report lands in the folder (checked every 30 minutes). "
                "Settings → Data quality lists what was found.</div></div>", unsafe_allow_html=True)


def pct1(x):
    return "–" if x is None or x != x else f"{x * 100:.1f}%"


def kpi_html(c):
    t = c.get("target")
    if c.get("text") is not None:
        big = f"<div class='val num'>{esc(c['text'])}</div>"
    elif c.get("count") is not None:
        big = f"<div class='val num'>{c['count']:,}</div>"
    elif c.get("value") is None or c["value"] != c["value"]:
        return ""
    else:
        big = f"<div class='val num {c.get('status', '')}'>{c['value'] * 100:.1f}%</div>"
    lines = "".join(f"<span>{esc(str(k))} <b class='num{' bad' if (t and v is not None and v == v and v < t) else ''}'>{pct1(v)}</b></span>"
                    for k, v in c.get("lines", []) if k)
    if c.get("pen"):
        lines += f"<span class='pen'>{esc(c['pen'][0])} <b class='num'>{pct1(c['pen'][1])}</b></span>"
    bar = ""
    if t and c.get("value") is not None:
        m = max(t, c["value"], 1e-9)
        bar = (f"<div class='bar'><i class='{c.get('status', '')}' style='width:{min(100, c['value'] / m * 100):.1f}%'></i>"
               f"<em style='left:{min(100, t / m * 100):.1f}%'></em></div>")
    foot = f"<div class='foot num'><span>{'Target ' + format(t, '.0%') if t else ''}</span><span>{esc(c.get('sub', ''))}</span></div>"
    return (f"<div class='kpi'><div class='lbl'>{esc(c['title'])}</div><div class='per'>{esc(c['per'])}</div>{big}"
            f"<div class='pl'>{lines}</div>{bar}{foot}</div>")


SCOPE = " · ".join(x for x in (V.PAGE_LABEL[PAGE], (ZM or "").title(), (RM or "").title()) if x)
st.markdown(f"<div class='scopebar'>{esc(SCOPE)}</div><div class='kpis'>{''.join(kpi_html(c) for c in M.kpis(pack, PAGE, ZM, RM))}</div>",
            unsafe_allow_html=True)

# --------------------------------------------------------------------------- #
# main panel + chat
# --------------------------------------------------------------------------- #
if S["show_chat"]:
    left, right = st.columns([1, 0.37], gap="medium")
else:
    left, right = st.container(), None
SC = f"{PAGE[:3]}z{ZM or 'all'}r{RM or 'all'}"      # widget-key suffix per scope

TAIL = ["Mail", "Reports", "Power BI", "Settings"]
PAGE_TABS = {"OVERALL": ["Overview", "Dealers", "Region / State", "Penetration", "Payout"],   # fixed per page
             "ROYAL ENFIELD": ["Overview", "Dealers", "Region / State", "First year", "Penetration", "Retention detail", "Payout"],
             "VOLVO": ["Overview", "Dealers", "Region / State", "Penetration", "Retention detail", "Open cases", "Payout"]}
SCORE_CFG = {c: st.column_config.ProgressColumn(c, format="percent", min_value=0.0, max_value=1.0)
             for c in ("Retention YTD", "Penetration YTD")}
SCORE_CFG.update({c: st.column_config.NumberColumn(c, format="percent") for c in
                  ("Last month", "MTD", "MTD first year", "First year YTD", "Target", "Penetration latest",
                   "Conversion MTD (due so far)", "Same date last month", "MTD first year (whole month)")})
SCORE_CFG.update({"Gap (pts)": st.column_config.NumberColumn("Gap (pts)", format="%+.1f"),
                  "Due YTD": st.column_config.NumberColumn("Due YTD", format="localized"),
                  "Due MTD (FY)": st.column_config.NumberColumn("Due MTD (FY)", format="localized"),
                  "NOP to target": st.column_config.NumberColumn("To target", format="localized")})


def score_table(sc: pd.DataFrame, level: str, key: str):
    """Management scorecard: progress bars for retention / penetration, click a row to drill down."""
    if sc.empty:
        st.caption("No data in this scope."); return
    d = sc.copy()
    for c in d.columns:
        if c in SCORE_CFG: d[c] = pd.to_numeric(d[c], errors="coerce")     # blanks, not "None"
    if "RAG" in d: d["RAG"] = d["RAG"].map(lambda v: RAG_ICON.get(v, v))
    show = [c for c in d.columns if not c.startswith("_") and c not in ("Level",)]
    h = min(520, 38 + 35 * len(d))
    ev = st.dataframe(d[show], hide_index=True, width="stretch", height=h, column_config=SCORE_CFG,
                      on_select="rerun", selection_mode="single-row", key=f"sc{key}")
    rows = ev.selection.rows if ev and hasattr(ev, "selection") else []
    if rows:
        r = sc.iloc[rows[0]]
        token = f"{rows[0]}:{r['Name']}"
        if S["handled"].get(key) != token:
            S["handled"][key] = token
            if level == "OEM":
                S["_goto_page"] = next((k for k, v in V.PAGE_LABEL.items() if v == r["Name"]), "OVERALL")
            elif level == "ZM":
                S["_goto_zm"] = r["_id"]
            elif level == "RM":
                S["_goto_rm"] = r["_id"]
            elif level == "Dealer" and r.get("Level") == "Group":
                o = next((k for k, v in V.PAGE_LABEL.items() if v == r.get("OEM")), PAGE)
                S["focus"] = ("group", f"{o}|{str(r['_id']).removesuffix(' (group)')}")
            elif level == "Dealer":
                S["focus"] = ("dealer", str(r["_id"]))
            st.rerun()
    st.caption({"OEM": "Click an OEM to open its page", "ZM": "Click a ZM to drill into their RMs",
                "RM": "Click an RM to see their dealers", "Dealer": "Click a dealer for its dashboard · click a Σ group row for the group's combined dashboard"}[level])


def mini_list(df, title, good):
    if df is None or df.empty: return ""
    rows = "".join(f"<div class='ml'><span class='n' title='{esc(str(r['Name']))}'>{esc(str(r['Name']))}</span>"
                   f"<span class='v num {'good' if good else 'bad'}'>{pct1(r.get('Retention YTD') if r.get('Retention YTD') == r.get('Retention YTD') and r.get('Retention YTD') is not None else r.get('Conversion MTD (due so far)', r.get('MTD first year')))}</span>"
                   f"<span class='g num'>{r['Gap (pts)']:+.0f} pts</span></div>" for _, r in df.iterrows())
    return f"<div class='mlist'><div class='mh'>{esc(title)}</div>{rows}</div>"


def overview_view():
    v = M.overview(pack, PAGE, ZM, RM)
    st.markdown(f"<div class='view-h'>{esc(v['title'])}</div><p class='lead'>{esc(v['asof'])}</p>", unsafe_allow_html=True)
    export_bar(M.to_report(v), f"ovx{SC}")
    if v["messages"]:
        st.markdown("<div class='msgs'>" + "".join(f"<div>{_md(x)}</div>" for x in v["messages"]) + "</div>", unsafe_allow_html=True)
    c1, c2 = st.columns([1.55, 1], gap="medium")
    with c1:
        if v["trend"] is not None:
            st.plotly_chart(to_plotly(v["trend"]), width="stretch", key=f"tr{SC}",
                            config={"displaylogo": False, "displayModeBar": False, "responsive": True})
    with c2:
        st.markdown(mini_list(v["attention"], "Needs attention", False) + mini_list(v["best"], "Best performers", True),
                    unsafe_allow_html=True)
    if len(v["months"]):
        st.markdown("<div class='sec-h'>" + ("Month on month by OEM - retention and penetration" if PAGE == "OVERALL" else
                    "Month on month - first year, other year, overall") + "</div>", unsafe_allow_html=True)
        mcfg = {c: st.column_config.NumberColumn(c, format="percent") for c in v["months"].columns if str(c).endswith("%")}
        mcfg.update({c: st.column_config.NumberColumn(c, format="localized") for c in
                     ("FY due", "FY renewed", "OY due", "OY renewed", "Total due", "Total renewed")})
        mcfg["Change (pts)"] = st.column_config.NumberColumn("Change (pts)", format="%+.1f")
        st.dataframe(v["months"], hide_index=True, width="stretch", column_config=mcfg, height=38 + 35 * len(v["months"]))
    st.markdown(f"<div class='sec-h'>{esc(v['level'])} scorecard - retention and penetration</div>", unsafe_allow_html=True)
    score_table(v["score"], v["level"], f"ov{SC}")
    st.caption(v["note"])
    if v.get("follow_up") is not None:                        # RM / ZM scope: action list
        st.divider()
        render(v["follow_up"], f"fu{SC}")


def rm_dashboard(rm):
    return entity_report("rm", rm) if PAGE == "OVERALL" else V.oem_dashboard(pack, PAGE, None, rm)


with left:
    with st.container(key="mainpanel"):
        # fixed slot above the tabs: opening/closing a dashboard never shifts the tabs, so the selected tab is kept
        focus_slot = st.container(key="focusslot")
        TABS = PAGE_TABS.get(PAGE, ["Overview", "Dealers", "Region / State", "Penetration", "Retention detail", "Payout"])
        TABS = TABS + list(FILE_TABS) + TAIL           # new report types are added after the standard tabs
        tabs = dict(zip(TABS, st.tabs(TABS)))

        HAS_RET = available("retention")
        with tabs["Overview"]:
            overview_view() if HAS_RET else missing("retention")

        with tabs["Dealers"]:
          if not HAS_RET:
            missing("retention")
          else:
            c1, c2 = st.columns([2, 1.3])
            q = c1.text_input("Search", placeholder="Dealer name or code", label_visibility="collapsed", key=f"dq{SC}")
            frag = c2.multiselect("RAG", ["Red", "Amber", "Green"], placeholder="All RAG", label_visibility="collapsed", key=f"drag{SC}")
            _, dsc = M.scorecard(pack, PAGE, ZM, RM, dealers=True)
            if len(dsc):
                if q: dsc = dsc[dsc["Name"].astype(str).str.contains(q, case=False, regex=False)
                                | dsc["_id"].astype(str).str.contains(q, case=False, regex=False)]
                if frag: dsc = dsc[dsc["RAG"].isin(frag) | (dsc.get("Level", "") == "Group")]
            glist = M.group_list(pack, PAGE, ZM)
            if glist:
                gsel = st.selectbox("Dealer group - combined dashboard", [None] + glist, key=f"gsel{SC}",
                                    format_func=lambda x: "Choose a clubbed dealer group…" if x is None else f"Σ {x[1]} · {OEM_LABEL.get(x[0], x[0])} · {len(M.group_codes(pack, *x))} codes")
                if gsel:
                    render(M.group_dashboard(pack, *gsel), f"grp{gsel[0]}{gsel[1]}")
                    st.divider()
            st.markdown(f"<div class='sec-h'>Dealers - {esc(SCOPE)} · {len(dsc)} rows</div>", unsafe_allow_html=True)
            dex = Report(f"Dealers - {SCOPE}", "Retention and penetration per dealer; clubbed groups show each code, then the total")
            if len(dsc): dex.table(dsc.drop(columns=[c for c in dsc.columns if c.startswith("_")]))
            export_bar(dex, f"dlx{SC}{q}{frag}")
            score_table(dsc, "Dealer", f"dl{SC}")
            with st.expander("Dealer dashboard · dealer meeting deck · several dealers together"):
                bk = BOOK if PAGE == "OVERALL" else BOOK[BOOK["oem"] == PAGE]
                if RM: bk = bk[bk["RM"] == RM]
                dl = {x.Code: f"{x.Dealer} · {x.Code} · {x.OEM} · {str(x.RM).title()}" for x in bk.itertuples()}
                picks = st.multiselect("Dealers", list(dl), format_func=lambda x: dl.get(x, x), key=f"dd{SC}",
                                       placeholder="Pick one dealer for its dashboard, or several for a combined report")
                if len(picks) == 1:
                    mode = st.radio("View", ["Dealer dashboard", "Dealer meeting deck"], horizontal=True, label_visibility="collapsed",
                                    key=f"ddm{SC}")
                    render(A.dealer_report(pack, picks[0]) if mode == "Dealer dashboard" else ra.meeting_deck(pack, picks[0]),
                           f"dd1{picks[0]}{mode[:3]}")
                elif len(picks) > 1:
                    render(ra.selection_report(pack, picks), f"ddn{'-'.join(picks)[:60]}")
                else:
                    st.caption("The meeting deck is dealer-facing: anonymised peers and the dealer's own unpaid commission; no RM visits or defaulter labels.")

        with tabs["Region / State"]:
          if not HAS_RET:
            missing("retention")
          else:
            c1, c2, c3 = st.columns([1, 1.3, 1.6])
            by = c1.radio("Group by", ["Region", "State"], horizontal=True, key=f"geob{SC}",
                          index=0 if PAGE == "ROYAL ENFIELD" else 1,
                          help="Royal Enfield: OEM regions (N1, S2 ...). Cars: EIBL branch as the region.")
            gd = M.dealer_geo(pack, PAGE, ZM, RM)
            areas = sorted(set(gd[by])) if len(gd) else []
            area = c2.selectbox("Area", ["All"] + areas, key=f"geoa{SC}{by}")
            show = c3.radio("Dealers", list(M.SHOW), format_func=lambda k: M.SHOW[k].replace(" dealers", ""), horizontal=True,
                            key=f"geos{SC}", help="Critical = Red with at least the minimum base; performing = at or above target")
            render(M.geo_report(pack, PAGE, by, ZM, RM, None if area == "All" else area, show), f"geo{SC}{by}{area}{show}")

        if "Retention detail" in tabs:
            with tabs["Retention detail"]:
                render(V.oem_dashboard(pack, PAGE, ZM, RM), f"od{SC}", skip_kpis=True) if HAS_RET else missing("retention")
        if "First year" in tabs:
            with tabs["First year"]:
                render(V.re_first_year(pack, ZM, RM), f"fy{SC}") if available("first_year") else missing("first_year")
        if "Open cases" in tabs:
            with tabs["Open cases"]:
                if available("open_cases"):
                    rep_ = Report(f"Volvo open cases - {SCOPE}", "Expired and not renewed - the chase list, aggregated")
                    render(rep_.extend(A.volvo_open_section(pack, ZM, RM)), f"oc{SC}")
                else:
                    missing("open_cases")
        if "Penetration" in tabs:
            with tabs["Penetration"]:
                render(M.penetration_page(pack, PAGE, ZM, RM), f"pen{SC}") if available("penetration") else missing("penetration")

        if "Payout" in tabs:
          with tabs["Payout"]:
           if not available("payout"):
            missing("payout")
           else:
            p1, p2 = st.tabs(["Summary", "Defaulter RMs"])
            with p1:
                render(V.payout_page(pack, PAGE, ZM), f"pay{SC}")
            with p2:
                render(V.defaulter_report(pack, PAGE, ZM), f"def{SC}")

        # one tab per spreadsheet the app does not know yet, named after the file
        for _tn, _path in FILE_TABS.items():
            with tabs[_tn]:
                from mis import explore as X
                _sh = X.sheets(_path)
                st.markdown(f"<p class='lead'>New report found in Google Drive: <b>{esc(os.path.basename(_path))}</b>. "
                            "The layout is read automatically - pick a sheet, then how to group and what to measure.</p>",
                            unsafe_allow_html=True)
                if not _sh:
                    st.caption("This file could not be opened."); continue
                k = hashlib.md5(_path.encode()).hexdigest()[:8]
                c1, c2, c3, c4 = st.columns([1.3, 1.2, 1.2, .8])
                sh = c1.selectbox("Sheet", _sh, key=f"xs{k}")
                try:
                    df = X.read_table(_path, sh)
                except Exception as e:
                    st.caption(f"Could not read this sheet: {type(e).__name__}"); continue
                if df.empty:
                    st.caption("This sheet is empty."); continue
                dims, meas, _dates = X.roles(df)
                by = c2.selectbox("Group by", [None] + dims, format_func=lambda x: "(no grouping)" if x is None else x, key=f"xb{k}{sh}",
                                  index=1 if dims else 0)
                me = c3.selectbox("Measure", [None] + meas, format_func=lambda x: "Count of rows" if x is None else x, key=f"xm{k}{sh}",
                                  index=1 if meas else 0)
                ag = c4.selectbox("As", ["sum", "mean", "max", "min", "count"], key=f"xa{k}{sh}", disabled=me is None)
                q = st.text_input("Filter rows containing", key=f"xq{k}{sh}", placeholder="dealer, RM, region…")
                if q:
                    df = df[df.astype(str).apply(lambda r: r.str.contains(q, case=False, regex=False).any(), axis=1)]
                render(X.report(os.path.basename(_path), sh, df, by, me, ag, q or None), f"x{k}{sh}{by}{me}{ag}{q}")

        with tabs["Mail"]:
            from mis import mailer as MAIL
            cfg = MAIL.smtp_config(lambda k, d=None: secret(k, d))
            directory = S.get("directory_df")
            if directory is None:
                directory = pack.get("dealer_directory")
            c1, c2, c3 = st.columns([1.6, 1.2, 1.2])
            with c1:
                up = st.file_uploader("Dealer directory (Excel)", type=["xlsx", "xls"], key="dirup",
                                      help="Columns: Dealer code, Dealer email, RM email, ZM email (optional). Or put a file "
                                           "named 'Dealer Directory ...' in the Drive folder.")
                if up is not None:
                    try:
                        S["directory_df"] = directory = MAIL.load_directory(up)
                    except Exception as e:
                        st.error(f"Could not read the directory: {e}")
            with c2:
                if "dir_tpl" not in S:
                    if st.button("Build directory template", key="dirtplb"):
                        S["dir_tpl"] = MAIL.directory_template(pack); st.rerun()
                else:
                    st.download_button("⬇ Directory template (all codes pre-filled)", S["dir_tpl"], file_name="Dealer Directory.xlsx",
                                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dirtpl")
            with c3:
                ok_smtp = bool(cfg["user"] and cfg["password"])
                st.markdown(f"<div class='note'>Sender: <b>{esc(cfg['from'] or 'not set')}</b><br>"
                            f"SMTP: {'ready' if ok_smtp else 'not configured - add SMTP_USER / SMTP_PASSWORD to secrets'}<br>"
                            f"Test address: {esc(cfg['test_to'] or 'not set (MAILER_TEST_TO)')}</div>", unsafe_allow_html=True)
            if directory is None or directory.empty:
                st.info("Upload the dealer directory (or use the template) to preview and send mails.")
            else:
                st.caption(f"Directory: {len(directory)} codes · {int(directory['dealer_emails'].map(bool).sum())} with a dealer e-mail · "
                           f"{int(directory['rm_emails'].map(bool).sum())} with an RM e-mail")
                cc_zm = st.toggle("CC the ZM as well", value=cfg["cc_zm"], key="cczm")
                m1, m2 = st.tabs(["Payout mail (bulk)", "Performance mail"])
                for kind, holder in (("payout", m1), ("performance", m2)):
                    with holder:
                        if kind == "payout":
                            pdl = MAIL.payout_dealers(pack, ZM, RM, PAGE)
                            st.markdown(f"<p class='lead'>Every dealer in <b>{esc(SCOPE)}</b> with <b>one or more unpaid invoices</b> - "
                                        "one mail each listing their invoices, the reason and the action needed. RM in CC.</p>",
                                        unsafe_allow_html=True)
                            codes = list(pdl["code"]) if len(pdl) else []
                            listing = pdl.rename(columns={"code": "Code", "name": "Dealer", "oem": "OEM", "rm": "RM", "zm": "ZM",
                                                          "lines": "Invoice lines", "value": "Pending value"}) if len(pdl) else pdl
                        else:
                            _, dsc = M.scorecard(pack, PAGE, ZM, RM, dealers=True)
                            ids = [str(x).upper() for x in (dsc["_id"] if len(dsc) else [])]
                            if PAGE in ("OVERALL", "ROYAL ENFIELD"):
                                snap = pack.get("re_dealer_snapshot")
                                if len(snap):
                                    if ZM: snap = snap[snap["zm"] == ZM]
                                    if RM: snap = snap[snap["rm"] == RM]
                                    ids += list(snap["dealer_code"].astype(str).str.upper())
                            codes = [c for c in dict.fromkeys(ids) if c in set(directory["code"])]
                            st.markdown(f"<p class='lead'>Each dealer's own retention, penetration and renewals to target for "
                                        f"<b>{esc(SCOPE)}</b>. Sent automatically whenever a newer report lands in Drive "
                                        "(GitHub auto-mailer); you can also send it now from here.</p>", unsafe_allow_html=True)
                            listing = directory[directory["code"].isin(codes)][["code", "name"]].rename(columns={"code": "Code", "name": "Dealer"})
                        if len(listing):
                            dm = directory.set_index("code")
                            listing["Dealer e-mail"] = listing["Code"].map(lambda c: "; ".join(dm["dealer_emails"].get(c, [])) if c in dm.index else "")
                            listing["RM e-mail (CC)"] = listing["Code"].map(lambda c: "; ".join(dm["rm_emails"].get(c, [])) if c in dm.index else "")
                            missing = int((listing["Dealer e-mail"] == "").sum())
                            st.caption(f"{len(listing)} dealers · {missing} without a dealer e-mail (they are skipped)")
                            st.dataframe(listing, hide_index=True, width="stretch", height=min(320, 38 + 35 * len(listing)))
                            pv = st.selectbox("Preview", list(listing["Code"]), format_func=lambda c: f"{c} · {listing.set_index('Code')['Dealer'].get(c, '')}",
                                              key=f"pv{kind}{SC}")
                            if pv:
                                mm = MAIL.build(pack, directory, kind, [pv], cc_zm)
                                if mm:
                                    st.markdown(f"<div class='note'><b>To:</b> {esc(', '.join(mm[0]['to']) or '-')} &nbsp; "
                                                f"<b>CC:</b> {esc(', '.join(mm[0]['cc']) or '-')}<br><b>Subject:</b> {esc(mm[0]['subject'])}</div>",
                                                unsafe_allow_html=True)
                                    with st.container(border=True):
                                        st.html(mm[0]["html"])
                            b1, b2, b3 = st.columns([1, 1.4, 1.2])
                            if b1.button("Send test to me", key=f"t{kind}{SC}", disabled=not ok_smtp,
                                         help="Builds the first 3 mails and sends them to MAILER_TEST_TO only"):
                                mails = MAIL.build(pack, directory, kind, codes[:3], cc_zm)
                                try:
                                    S[f"maillog_{kind}"] = MAIL.send(mails, cfg, mode="test")
                                except Exception as e:
                                    st.error(f"Test send failed: {type(e).__name__}: {e}")
                            sure = b2.checkbox(f"I have checked the preview - send to all {len(listing) - missing} dealers", key=f"ok{kind}{SC}")
                            if b3.button("Send now", type="primary", key=f"s{kind}{SC}", disabled=not (sure and ok_smtp)):
                                bar = st.progress(0.0, "Sending…")
                                mails = MAIL.build(pack, directory, kind, codes, cc_zm)
                                try:
                                    S[f"maillog_{kind}"] = MAIL.send(mails, cfg, mode="live",
                                                                     progress=lambda f, c: bar.progress(f, f"Sending {c}…"))
                                except Exception as e:
                                    st.error(f"Sending failed: {type(e).__name__}: {e}")
                                bar.empty()
                            lg = S.get(f"maillog_{kind}")
                            if lg is not None and len(lg):
                                st.markdown("<div class='sec-h'>Last run</div>", unsafe_allow_html=True)
                                st.caption(" · ".join(f"{k}: {v}" for k, v in lg["status"].value_counts().items()))
                                st.dataframe(lg, hide_index=True, width="stretch", height=min(300, 38 + 35 * len(lg)))
                                st.download_button("⬇ Send log (CSV)", lg.to_csv(index=False).encode(), file_name=f"mail_log_{kind}.csv",
                                                   mime="text/csv", key=f"lg{kind}")
                        else:
                            st.caption("No dealers to mail in this scope.")

        with tabs["Reports"]:
            st.markdown(f"<p class='lead'>Every report follows the scope in the top bar (<b>{esc(SCOPE)}</b>) and exports to "
                        "HTML, PDF, PowerPoint or CSV. For anything else, ask the assistant.</p>", unsafe_allow_html=True)
            CAT = [("overview", "Performance overview", "KPIs, trend, scorecard one level down, attention and best lists."),
                   ("detail", "Retention detail", "Month-wise FY / OY retention, zones, RMs, priority dealers, movers."),
                   ("penetration", "Penetration review", "Policies per vehicle sold by month, ZM, RM and dealer (groups clubbed)."),
                   ("group1", "Dealer group - combined dashboard", "One clubbed group: totals, month on month, each code then the total, payouts."),
                   ("groups", "Dealer groups (clubbed)", "Each code of every clubbed group, then the group total."),
                   ("matrix", "Retention x penetration", "Dealers in four quadrants with the action for each."),
                   ("region", "Region-wise with dealers", "Every region (RE) / branch (cars) and its dealers ranked - choose all, critical or performing."),
                   ("state", "State-wise with dealers", "Every state and its dealers ranked - choose all, critical or performing."),
                   ("zm", "ZM review", "One ZM's whole zone across OEMs (pick a zone in the top bar)."),
                   ("rm", "RM review", "One RM's book with every parameter (pick an RM in the top bar)."),
                   ("dealer", "Dealer review", "One dealer: trend, peers, penetration, open cases, payouts."),
                   ("meeting", "Dealer meeting deck", "Dealer-facing: performance, anonymised peers, agreed actions."),
                   ("defaulters", "Defaulter RMs", "RMs with 2 or more unpaid invoices, why, and the dealers behind them."),
                   ("payout", "Payout status", "Pending invoices and what is already paid.")]
            if PAGE == "ROYAL ENFIELD": CAT.insert(3, ("refy", "First-year conversion", "RE first year by region, RM and dealer."))
            if PAGE == "VOLVO": CAT.insert(3, ("open", "Open cases", "Volvo policies expired and not renewed."))
            if PAGE == "OVERALL":
                CAT += [(f"ra:{i}", f"{lab} (all OEMs)", d) for i, lab, d in ra.REPORTS
                        if i in ("zone", "boom", "under", "fy", "movers", "quality")]
            labels = {k: lab for k, lab, _ in CAT}
            c1, c2 = st.columns([1.4, 1.6])
            rid = c1.selectbox("Report", list(labels), format_func=lambda x: labels[x], key=f"rpt{PAGE}")
            who = None
            if rid == "group1":
                gl2 = M.group_list(pack, PAGE, ZM)
                who = c2.selectbox("Group", gl2, format_func=lambda x: f"Σ {x[1]} · {OEM_LABEL.get(x[0], x[0])}", index=None,
                                   placeholder="Choose a group", key=f"rptg{SC}")
            if rid in ("region", "state"):
                who = c2.radio("Dealers", list(M.SHOW), format_func=lambda k: M.SHOW[k], horizontal=True, key=f"rptshow{SC}")
            if rid in ("meeting", "dealer"):
                bk = BOOK if PAGE == "OVERALL" else BOOK[BOOK["oem"] == PAGE]
                if RM: bk = bk[bk["RM"] == RM]
                dl2 = {x.Code: f"{x.Dealer} · {x.Code} · {x.OEM}" for x in bk.itertuples()}
                who = c2.selectbox("Dealer", list(dl2), format_func=lambda x: dl2.get(x, x), index=None, placeholder="Choose dealer", key=f"rptd{SC}")
            st.caption(next(d for k, _, d in CAT if k == rid))
            if st.button("Build report", type="primary", key="rptgo"):
                S["built"] = (rid, PAGE, ZM, RM, who)
            if S.get("built") and S["built"][1:4] == (PAGE, ZM, RM):
                b_rid, _, _, _, b_who = S["built"]
                if b_rid == "overview": rep = M.to_report(M.overview(pack, PAGE, ZM, RM))
                elif b_rid == "detail":
                    rep = V.overall_dashboard(pack, ZM) if PAGE == "OVERALL" else V.oem_dashboard(pack, PAGE, ZM, RM)
                elif b_rid == "penetration": rep = M.penetration_page(pack, PAGE, ZM, RM)
                elif b_rid == "groups": rep = M.group_report(pack, PAGE, ZM)
                elif b_rid == "group1": rep = M.group_dashboard(pack, *b_who) if b_who else Report("Dealer group").p("Choose a group.")
                elif b_rid in ("region", "state"): rep = M.geo_report(pack, PAGE, b_rid.title(), ZM, RM, None, b_who or "all")
                elif b_rid == "matrix": rep = M.ret_pen_matrix(pack, PAGE, ZM, RM)
                elif b_rid == "zm": rep = M.zm_review(pack, ZM, PAGE) if ZM else Report("ZM review").p("Pick a zone in the top bar.")
                elif b_rid == "rm": rep = rm_dashboard(RM) if RM else Report("RM review").p("Pick a zone, then an RM, in the top bar.")
                elif b_rid == "dealer": rep = A.dealer_report(pack, b_who) if b_who else Report("Dealer review").p("Choose a dealer.")
                elif b_rid == "meeting": rep = ra.meeting_deck(pack, b_who) if b_who else Report("Dealer meeting deck").p("Choose a dealer.")
                elif b_rid == "defaulters": rep = V.defaulter_report(pack, PAGE, ZM)
                elif b_rid == "payout": rep = V.payout_page(pack, PAGE, ZM)
                elif b_rid == "refy": rep = V.re_first_year(pack, ZM, RM)
                elif b_rid == "open": rep = Report(f"Volvo open cases - {SCOPE}").extend(A.volvo_open_section(pack, ZM, RM))
                else: rep = ra.build(pack, b_rid[3:], ZM, None)
                render(rep, f"rpt{b_rid}{SC}{b_who}")
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

        with tabs["Power BI"]:
            b1, b2, b3 = st.tabs(["Explore (matrix)", "Download for Power BI", "Embedded Power BI report"])
            with b1:
                st.markdown("<p class='lead'>Slice the retention data like a Power BI matrix: pick rows, columns, a measure "
                            "and a period. The result charts itself and exports like any report.</p>", unsafe_allow_html=True)
                c1, c2, c3, c4 = st.columns(4)
                dims = list(V.DIMS)
                rows_ = c1.selectbox("Rows", dims, index=dims.index("RM") if PAGE != "OVERALL" else 0, key=f"pbr{SC}")
                cols_ = c2.selectbox("Columns", ["(none)"] + dims, index=0, key=f"pbc{SC}")
                meas = c3.selectbox("Measure", V.MEASURES, key=f"pbm{SC}")
                per = c4.selectbox("Period", V.PERIODS, index=3 if cols_ == "Month" else 0, key=f"pbp{SC}")
                _, xrep = V.explore(pack, PAGE, rows_, cols_, meas, per, ZM)
                render(xrep, f"pbx{SC}{rows_}{cols_}{meas}{per}")
            with b2:
                st.markdown("<p class='lead'>A ready star-schema model of this MIS drop for Power BI Desktop: fact and dimension "
                            "tables (Excel workbook + CSVs), every measure in DAX (retention %, YTD, MTD, last month, RAG, "
                            "renewals to target, unpaid invoices, defaulter RM, penetration), the relationships and a "
                            "step-by-step guide. Open Power BI Desktop → Get data → Excel → tick all sheets.</p>",
                            unsafe_allow_html=True)
                if "pbi_zip" not in S or S.get("pbi_sig") != DATA_SIG:
                    if st.button("Build Power BI package", type="primary", key="pbibuild"):
                        with st.spinner("Building the model…"):
                            S["pbi_zip"], S["pbi_sig"] = V.powerbi_package(pack), DATA_SIG
                        st.rerun()
                else:
                    st.download_button("⬇ Download MIS_PowerBI_model.zip", S["pbi_zip"], file_name=f"MIS_PowerBI_model_{asof}.zip",
                                       mime="application/zip", type="primary", key="pbidl")
                with st.expander("Tables in the model"):
                    st.dataframe(pd.DataFrame([{"Table": k, "Rows": len(v), "Columns": ", ".join(map(str, v.columns))}
                                               for k, v in V.powerbi_tables(pack).items()]), hide_index=True, width="stretch")
                with st.expander("DAX measures"):
                    st.code(V.DAX, language="sql")
                st.caption("Ask the assistant for anything else in DAX or Power Query (M) - e.g. \"write a DAX measure for "
                           "first-year retention vs same date last month\".")
            with b3:
                url = st.text_input("Power BI report link", value=S.get("pbi_url") or secret("POWERBI_URL", "") or "",
                                    placeholder="https://app.powerbi.com/view?r=…  (File → Embed report → Publish to web / Website or portal)",
                                    key="pbiurl")
                S["pbi_url"] = url
                if url.startswith("https://app.powerbi.com/") or url.startswith("https://msit.powerbi.com/"):
                    st.iframe(url, height=640)
                else:
                    st.markdown("<p class='lead'>Paste the embed link of a report published from Power BI to show it here, next "
                                "to the assistant. Put it in the app secrets as <code>POWERBI_URL</code> to make it permanent. "
                                "Links that need a Microsoft sign-in only show for viewers with Power BI access.</p>",
                                unsafe_allow_html=True)

        with tabs["Settings"]:
            data_controls()
            # every sheet of every file in the Drive folder, whatever the app uses it for
            st.markdown("<div class='sec-h'>Drive files - every file, every sheet</div>", unsafe_allow_html=True)
            from mis import explore as X
            _all = sorted(f for f in os.listdir(DATA_DIR) if f.lower().endswith((".xlsx", ".xlsb", ".xlsm")) and not f.startswith(("~$", "_")))
            _used = {os.path.basename(f.path): k for k, f in pack.sources.items()}
            st.caption(f"{len(_all)} files in the Google Drive folder · " + " · ".join(
                f"{n} ({FILE_TYPES[_used[n]][1] if n in _used else 'own tab'})" for n in _all))
            c1, c2 = st.columns([1.6, 1.2])
            fsel = c1.selectbox("File", _all, key="dfile") if _all else None
            if fsel:
                fpath = os.path.join(DATA_DIR, fsel)
                shs = X.sheets(fpath)
                ssel = c2.selectbox(f"Sheet ({len(shs)} in this file)", shs, key=f"dsheet{fsel}") if shs else None
                if ssel:
                    try:
                        sdf = X.read_table(fpath, ssel)
                    except Exception as e:
                        sdf = pd.DataFrame(); st.caption(f"Could not read this sheet: {type(e).__name__}")
                    if sdf.empty:
                        st.caption("This sheet is empty or holds only a chart / pivot layout.")
                    else:
                        dims, meas, _ = X.roles(sdf)
                        c3, c4, c5 = st.columns(3)
                        by = c3.selectbox("Group by", [None] + dims, format_func=lambda x: "(no grouping)" if x is None else x, key=f"dby{fsel}{ssel}")
                        me = c4.selectbox("Measure", [None] + meas, format_func=lambda x: "Count of rows" if x is None else x, key=f"dme{fsel}{ssel}")
                        ag = c5.selectbox("As", ["sum", "mean", "max", "min", "count"], key=f"dag{fsel}{ssel}", disabled=me is None)
                        render(X.report(fsel, ssel, sdf, by, me, ag), f"drv{fsel}{ssel}{by}{me}{ag}")
            st.markdown("<div class='sec-h'>AI assistant</div>", unsafe_allow_html=True)
            provs = ["gemini", "groq", "xai", "openrouter", "openai", "claude"]
            plabel = {"gemini": "Google Gemini (free key)", "groq": "Groq (free key, gsk_...)",
                      "xai": "xAI Grok (xai-... key)", "openrouter": "OpenRouter (free models)",
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
                S["free_model"] = st.text_input("Model (optional)", value=S.get("free_model", ""),
                                                placeholder="leave empty - the newest available model is chosen automatically")
                st.caption("Free key: aistudio.google.com/apikey (Gemini) or console.groq.com/keys (Groq).")
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
            st.caption("RAG, policies-to-target, flags and insights everywhere follow these criteria. "
                       "A defaulter RM has 2 or more unpaid invoices, for any reason.")
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
            label = {"dealer": "Dealer dashboard", "rm": "RM dashboard", "zm": "ZM dashboard", "answer": "Assistant answer",
                     "group": "Dealer group - combined dashboard"}[kind]
            c1.markdown(f"<div class='focus'><span class='k'>{label}</span></div>", unsafe_allow_html=True)
            if c2.button("✕ Close", key="close_focus", width="stretch"):
                S["focus"] = None; st.rerun()
            if kind == "answer":
                i = int(ident)
                if 0 <= i < len(S["chat"]) and S["chat"][i].get("report") is not None:
                    render(S["chat"][i]["report"], f"fa{i}")
            elif kind == "rm":
                render(rm_dashboard(ident), f"f{kind}{ident}{SC}")
            elif kind == "group":
                go, gg = ident.split("|", 1)
                render(M.group_dashboard(pack, go, gg), f"fgrp{ident}")
            else:
                render(entity_report(kind, ident), f"f{kind}{ident}")
            st.divider()

if right is not None:
  with right:
    with st.container(key="chatpanel"):
        on = bool(API_KEY)
        st.markdown(f"<div class='chead'><span class='dot{'' if on else ' off'}'></span><div><h2>Ask anything</h2>"
                    f"<p>{(('Claude · ' + esc(S['model']) + ' · effort ' + esc(S['effort'])) if S['provider'] == 'claude' else (esc(S['provider'].title()) + ' · ' + esc(PROVIDERS[S['provider']]['model']))) if on else 'Offline analyst - add a free Gemini key in Settings'}"
                    f"{' · ' + esc(V.PAGE_LABEL[PAGE]) if PAGE != 'OVERALL' else ''}{' · ' + esc(ZM.title()) if ZM else ''}</p></div></div>", unsafe_allow_html=True)
        box = st.container(height=620, border=False)
        with box:
            if not S["chat"]:
                st.markdown("<p class='note'>Chat like you would with Gemini - any question, any topic. Ask about an OEM, zone, "
                            "RM or dealer and it runs queries on the live MIS data, builds tables and charts, adds industry "
                            "context and web research on retention, and the answer can be exported.</p>", unsafe_allow_html=True)
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
        q = st.chat_input("Ask anything - or about any OEM, zone, RM or dealer…")
        if q:
            with box:
                with st.chat_message("user", avatar="🧑"):
                    st.markdown(q)
                with st.chat_message("assistant", avatar="📊"):
                    with st.status("Thinking…", expanded=False) as stt:
                        scope = ", ".join(x for x in ((f"OEM {V.PAGE_LABEL[PAGE]}" if PAGE != "OVERALL" else ""), (f"zone {ZM}" if ZM else "")) if x)
                        ask = q if not scope else f"{q}\n\n(Scope: if this is about the MIS data, {scope} unless the question says otherwise.)"
                        ans = run_agent(pack, ask, [{"role": m["role"], "content": m["content"]} for m in S["chat"]],
                                        api_key=API_KEY, provider=S["provider"],
                                        model=S["model"] if S["provider"] == "claude" else S.get("free_model", "").strip(), effort=S["effort"],
                                        on_step=lambda n, a: stt.update(label="Searching the web…" if n == "web_research" else f"Running {n}…"))
                        stt.update(label="Done", state="complete")
            S["chat"] += [{"role": "user", "content": q},
                          {"role": "assistant", "content": ans.text, "report": ans.report, "steps": ans.steps, "error": ans.error}]
            if ans.report is not None:          # plain chat replies stay in the chat; analyses open in the main panel
                S["focus"] = ("answer", str(len(S["chat"]) - 1))
            st.rerun()
        if S["chat"] and st.button("Clear conversation", key="clearchat"):
            S["chat"] = []; S["focus"] = None; st.rerun()
