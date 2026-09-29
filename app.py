"""MIS Agent - agentic retention MIS with OEM dashboards, insights, charts and HTML / PDF / PPT / CSV export.

Run locally:   streamlit run app.py
Deploy:        see README.md (Streamlit Community Cloud or Hugging Face Spaces - both free)
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time

import pandas as pd
import streamlit as st

from mis import analytics as A
from mis.agent import PROVIDERS, offline_answer, run_agent
from mis.charts import to_plotly
from mis.loaders import FILE_TYPES, load_folder
from mis.model import CATALOG, OEM_LABEL, OEMS, TARGETS
from mis.report import Report, display_df, export

st.set_page_config(page_title="MIS Agent", page_icon="📊", layout="wide", initial_sidebar_state="expanded")
st.markdown("""<style>
.block-container{padding-top:3.2rem;padding-bottom:3rem}
div[data-testid="stMetric"]{background:var(--secondary-background-color);border-radius:10px;padding:10px 12px;border:1px solid rgba(128,128,128,.18)}
div[data-testid="stMetricValue"]{font-size:1.55rem}
.small{opacity:.7;font-size:.85rem}
</style>""", unsafe_allow_html=True)


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
    st.title("📊 MIS Agent")
    with st.form("login"):
        p = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in"):
            if p == pw:
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


def folder_sig(folder):
    items = sorted((f, os.path.getsize(os.path.join(folder, f)), os.path.getmtime(os.path.join(folder, f)))
                   for f in os.listdir(folder) if f.lower().endswith((".xlsx", ".xlsb", ".xlsm")))
    return hashlib.md5(json.dumps(items).encode()).hexdigest()


@st.cache_resource(show_spinner=False, max_entries=3)
def get_pack(folder, sig):
    return load_folder(folder)


def sa_info():
    try:
        v = st.secrets["gcp_service_account"]
        return dict(v)
    except Exception:
        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        return json.loads(raw) if raw else None


def do_drive_sync():
    from mis.drive import sync
    bar = st.sidebar.progress(0.0, "Connecting to Google Drive…")
    try:
        got = sync(sa_info(), DATA_DIR, secret("DRIVE_FOLDER_ID"), progress=lambda f, n: bar.progress(f, f"Downloading {n[:40]}…"))
        st.session_state["last_sync"] = time.strftime("%d %b %H:%M")
        st.sidebar.success(f"Synced {len(got)} files from Drive")
    except Exception as e:
        st.sidebar.error(f"Drive sync failed: {type(e).__name__}: {e}")
    bar.empty()


with st.sidebar:
    st.markdown("## 📊 MIS Agent")
    src = st.radio("Data source", ["Google Drive", "Upload files"], horizontal=True,
                   index=0 if sa_info() else 1, help="Drive needs a service account in the app secrets (see README).")
    if src == "Google Drive":
        if not sa_info():
            st.warning("No Google service account configured - add `gcp_service_account` to secrets, or upload files.")
        else:
            auto = not os.listdir(DATA_DIR) and "synced_once" not in st.session_state
            if st.button("🔄 Sync latest MIS from Drive", use_container_width=True) or auto:
                st.session_state["synced_once"] = True
                do_drive_sync()
            if st.session_state.get("last_sync"):
                st.caption(f"Last sync {st.session_state['last_sync']}")
    else:
        up = st.file_uploader("Drop the day's MIS workbooks", type=["xlsx", "xlsb", "xlsm"], accept_multiple_files=True)
        if up:
            for f in up:
                with open(os.path.join(DATA_DIR, f.name), "wb") as fh:
                    fh.write(f.getbuffer())
            st.success(f"{len(up)} files saved")

files_present = [f for f in os.listdir(DATA_DIR) if f.lower().endswith((".xlsx", ".xlsb", ".xlsm"))]
if not files_present:
    st.title("📊 MIS Agent")
    st.info("No MIS files yet. Sync from Google Drive or upload the workbooks from the sidebar.")
    st.stop()

DATA_SIG = folder_sig(DATA_DIR)
with st.spinner("Reading MIS workbooks… (first load ~30-60 s, then cached)"):
    pack = get_pack(DATA_DIR, DATA_SIG)

# --------------------------------------------------------------------------- #
# settings: AI + targets
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.divider()
    with st.expander("🤖 AI model", expanded=False):
        default_p = secret("LLM_PROVIDER", "gemini")
        provider = st.selectbox("Provider", list(PROVIDERS), index=list(PROVIDERS).index(default_p) if default_p in PROVIDERS else 0,
                                format_func=lambda x: {"gemini": "Google Gemini (free key)", "groq": "Groq (free key)",
                                                       "openrouter": "OpenRouter (free models)", "openai": "OpenAI",
                                                       "anthropic": "Anthropic Claude"}[x])
        key_secret = secret("LLM_API_KEY") or secret(f"{provider.upper()}_API_KEY")
        api_key = st.text_input("API key", type="password", value="", placeholder="using key from secrets" if key_secret else "paste key (kept for this session only)")
        api_key = api_key or key_secret or ""
        model = st.text_input("Model", value=secret("LLM_MODEL", "") or PROVIDERS[provider]["model"])
        st.caption("No key? The agent still answers using the built-in offline analyst.")
    with st.expander("🎯 Targets", expanded=False):
        tgt = st.session_state.setdefault("targets", json.loads(secret("TARGETS_JSON", "null") or "null") or json.loads(json.dumps(TARGETS)))
        for o in OEMS:
            c1, c2, c3 = st.columns(3)
            tgt[o]["FY"] = c1.number_input(f"{OEM_LABEL[o][:10]} FY%", 0, 100, int(round(tgt[o]["FY"] * 100)), key=f"t{o}fy") / 100
            tgt[o]["OY"] = c2.number_input("OY%", 0, 100, int(round(tgt[o]["OY"] * 100)), key=f"t{o}oy") / 100
            tgt[o]["TOT"] = c3.number_input("Total%", 0, 100, int(round(tgt[o]["TOT"] * 100)), key=f"t{o}tot") / 100
        st.caption("RE FY 30% and Volvo 75% come from the MIS files; Audi/Skoda/VW targets are placeholders - set yours.")
    for o in OEMS:
        TARGETS[o].update(st.session_state["targets"][o])
    st.divider()
    st.caption(" · ".join(f"{k}: {v}" for k, v in pack.asof.items()))
    pins = st.session_state.setdefault("pins", [])
    if pins:
        st.markdown(f"**📌 Report pack ({len(pins)})**")
        for i, p_ in enumerate(pins):
            st.caption(f"{i + 1}. {p_.title}")
        combo = Report("MIS report pack", " · ".join(f"{k} as of {v}" for k, v in pack.asof.items()))
        for p_ in pins:
            combo.h(p_.title); combo.extend(p_)
        st.session_state["combo"] = combo
        if st.button("Clear pack", use_container_width=True):
            st.session_state["pins"] = []; st.rerun()


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def export_bar(rep: Report, key: str):
    cols = st.columns([1, 1, 1, 1, 1.3, 3])
    labels = {"html": "🌐 HTML", "pdf": "📄 PDF", "pptx": "📽️ PPT", "csv": "🧮 CSV"}
    cache = st.session_state.setdefault("exports", {})
    sig = hashlib.md5(f"{key}|{rep.title}|{len(rep.blocks)}|{DATA_SIG}|{json.dumps(TARGETS, sort_keys=True)}".encode()).hexdigest()[:12]
    for c, fmt in zip(cols, ("html", "pdf", "pptx", "csv")):
        ck = f"{sig}:{fmt}"
        if fmt in ("html", "csv") or ck in cache:
            if ck not in cache:
                cache[ck] = export(rep, fmt)
            data, fn, mime = cache[ck]
            c.download_button(labels[fmt], data, file_name=fn, mime=mime, key=f"dl{ck}", use_container_width=True)
        else:
            if c.button(labels[fmt], key=f"mk{ck}", use_container_width=True, help=f"Build the {fmt.upper()}"):
                with st.spinner(f"Building {fmt.upper()}…"):
                    cache[ck] = export(rep, fmt)
                st.rerun()
    if cols[4].button("📌 Add to pack", key=f"pin{sig}", use_container_width=True,
                      help="Collect several dashboards / answers and export them as one file"):
        st.session_state["pins"].append(rep); st.toast("Added to report pack"); st.rerun()


def render(rep: Report, key: str, show_title=True):
    if show_title:
        st.subheader(rep.title)
        if rep.subtitle: st.caption(rep.subtitle)
    export_bar(rep, key)
    for i, b in enumerate(rep.blocks):
        if b.kind == "heading":
            st.markdown(f"#### {b.title}")
        elif b.kind == "text":
            st.markdown(b.text)
        elif b.kind == "bullets":
            if b.title: st.markdown(f"**{b.title}**")
            st.markdown("\n".join(f"- {x}" for x in b.items))
        elif b.kind == "kpis":
            if b.title: st.caption(b.title)
            items = b.items
            for r0 in range(0, len(items), 6):
                cs = st.columns(min(6, len(items) - r0))
                for c, k in zip(cs, items[r0:r0 + 6]):
                    c.metric(str(k[0]), str(k[1]), help=str(k[2]) if len(k) > 2 and k[2] else None)
                    if len(k) > 2 and k[2]: c.caption(str(k[2]))
        elif b.kind == "table":
            if b.title: st.markdown(f"**{b.title}**")
            st.dataframe(display_df(b.df), hide_index=True, use_container_width=True,
                         height=min(420, 38 + 35 * len(b.df)))
        elif b.kind == "chart":
            st.plotly_chart(to_plotly(b.chart), use_container_width=True, key=f"ch{key}{i}",
                            config={"displaylogo": False})


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #
tabs = st.tabs(["🏠 Overview", "🏍️🚗 OEM dashboards", "🔍 Dealer / RM / ZM", "🤖 Ask the agent", "🧪 Data explorer",
                "📌 Report pack", "🩺 Data quality"])

with tabs[0]:
    render(A.overview_report(pack), "ov")

with tabs[1]:
    r = pack.get("retention")
    c1, c2, c3 = st.columns(3)
    oem = c1.selectbox("OEM", OEMS + ["ALL"], format_func=lambda x: OEM_LABEL.get(x, "Private car - all brands"))
    if oem == "ROYAL ENFIELD":
        zms = sorted(set(pack.get("re_dealer_snapshot").get("zm", pd.Series(dtype=str))) - {""})
        zm = c2.selectbox("ZM", ["All"] + zms)
        snap = pack.get("re_dealer_snapshot")
        rms = sorted(set(snap[snap["zm"] == zm]["rm"] if zm != "All" else snap.get("rm", pd.Series(dtype=str))) - {""})
    else:
        d = r[r["oem"] == oem] if oem != "ALL" else r[r["book"] == "Private car"]
        zm = c2.selectbox("ZM", ["All"] + sorted(set(d["zm"]) - {""}))
        rms = sorted(set(d[d["zm"] == zm]["rm"] if zm != "All" else d["rm"]) - {""})
    rm = c3.selectbox("RM", ["All"] + rms)
    rep = A.oem_report(pack, oem, None if zm == "All" else zm, None if rm == "All" else rm)
    render(rep, f"oem{oem}{zm}{rm}")

with tabs[2]:
    kind = st.radio("Look up", ["Dealer", "RM", "ZM"], horizontal=True)
    if kind == "Dealer":
        r = pack.get("retention"); s_ = pack.get("re_dealer_snapshot")
        opts = pd.concat([r[["dealer_code", "dealer_name", "oem"]].drop_duplicates("dealer_code"),
                          s_[["dealer_code", "dealer_name"]].assign(oem="ROYAL ENFIELD") if not s_.empty else pd.DataFrame()])
        opts = opts.drop_duplicates("dealer_code")
        lab = {row.dealer_code: f"{row.dealer_name} · {row.dealer_code} · {OEM_LABEL.get(row.oem, row.oem)}" for row in opts.itertuples()}
        dc = st.selectbox("Dealer (type to search)", list(lab), format_func=lambda x: lab[x], index=None, placeholder="Search dealer name or code")
        if dc: render(A.dealer_report(pack, dc), f"dl{dc}")
    else:
        role = kind.lower()
        r = pack.get("retention")
        names = sorted(set(r[role]) | set(pack.get("re_dealer_snapshot").get(role, pd.Series(dtype=str))) - {""})
        who = st.selectbox(kind, names, index=None, placeholder=f"Choose {kind}")
        if who:
            books = sorted(set(r[r[role] == who]["oem"]))
            snap = pack.get("re_dealer_snapshot")
            if not snap.empty and (snap[role] == who).any(): books = ["ROYAL ENFIELD"] + [b for b in books if b != "ROYAL ENFIELD"]
            books = list(dict.fromkeys(books))
            combo = Report(f"{kind} review - {who}", " · ".join(f"{k} as of {v}" for k, v in pack.asof.items()))
            for b in books:
                sub = A.oem_report(pack, b, zm=who if role == "zm" else None, rm=who if role == "rm" else None)
                combo.h(sub.title); combo.extend(sub)
            render(combo, f"{role}{who}")

with tabs[3]:
    st.markdown("Ask anything about the MIS - rankings, comparisons, trends, root causes, action plans. "
                "Every answer comes with its tables and charts and can be exported.")
    if not api_key:
        st.info("No AI key set - answers come from the built-in offline analyst. Add a free Gemini or Groq key in the sidebar "
                "(🤖 AI model) for full conversational analysis.", icon="ℹ️")
    hist = st.session_state.setdefault("chat", [])
    for i, m in enumerate(hist):
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
            if m.get("report") is not None:
                with st.expander("Tables, charts & export", expanded=(i == len(hist) - 1)):
                    render(m["report"], f"ans{i}", show_title=False)
            if m.get("steps"):
                st.caption("Tools used: " + " → ".join(s.split("(")[0] for s in m["steps"]))
    q = st.chat_input("e.g. Which Skoda dealers in Naman Singh's zone lost the most first-year renewals vs July?")
    if q:
        with st.chat_message("user"):
            st.markdown(q)
        with st.chat_message("assistant"):
            with st.status("Analysing…", expanded=False) as stt:
                ans = run_agent(pack, q, [{"role": m["role"], "content": m["content"]} for m in hist],
                                provider=provider, api_key=api_key, model=model,
                                on_step=lambda n, a: stt.update(label=f"Running {n}…"))
                stt.update(label="Done", state="complete")
            if ans.error: st.caption(f"Model error: {ans.error}")
        hist += [{"role": "user", "content": q}, {"role": "assistant", "content": ans.text, "report": ans.report, "steps": ans.steps}]
        st.rerun()
    if hist and st.button("Clear conversation"):
        st.session_state["chat"] = []; st.rerun()

with tabs[4]:
    avail = [k for k in CATALOG if not pack.get(k).empty]
    ds = st.selectbox("Dataset", avail, format_func=lambda k: f"{k}  ({len(pack.get(k)):,} rows)")
    st.caption(CATALOG[ds])
    df = pack.get(ds)
    c1, c2, c3, c4 = st.columns(4)
    cat_cols = [c for c in df.columns if df[c].dtype == object or df[c].dtype == bool]
    num_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c]) and df[c].dtype != bool]
    fcol = c1.selectbox("Filter column", ["(none)"] + cat_cols)
    fval = c2.multiselect("Values", sorted(df[fcol].astype(str).unique())[:500]) if fcol != "(none)" else []
    gby = c3.multiselect("Group by", cat_cols)
    mets = c4.multiselect("Sum", num_cols, default=[c for c in ("base", "ach") if c in num_cols])
    d = df[df[fcol].astype(str).isin(fval)] if fval else df
    if gby and mets:
        d = d.groupby(gby, as_index=False)[mets].sum()
        if "base" in d and "ach" in d: d["Ret %"] = d["ach"] / d["base"].where(d["base"] != 0)
        if "retail" in d and "new_nop" in d: d["Penetration %"] = d["new_nop"] / d["retail"].where(d["retail"] != 0)
    ex = Report(f"{ds} extract", CATALOG[ds]).table(d.head(5000), ds)
    if gby and mets and len(d) <= 60:
        from mis.charts import from_df
        ycol = "Ret %" if "Ret %" in d else mets[0]
        kind = st.radio("Chart", ["bar", "hbar", "line", "pie"], horizontal=True)
        ex.chart(from_df(d, gby[0], ycol, kind, f"{ycol} by {gby[0]}"))
    render(ex, f"ex{ds}", show_title=False)

with tabs[5]:
    if not st.session_state.get("pins"):
        st.info("Use **📌 Add to pack** under any dashboard or agent answer to collect a multi-section report, then export it here as one HTML / PDF / PPT / CSV.")
    else:
        render(st.session_state["combo"], "combo")

with tabs[6]:
    render(A.data_quality_report(pack), "dq")
    with st.expander("Files expected"):
        st.dataframe(pd.DataFrame([{"Type": v[1], "Book": v[2], "File name pattern": v[0]} for v in FILE_TYPES.values()]),
                     hide_index=True, use_container_width=True)
