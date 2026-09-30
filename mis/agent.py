"""The agentic analyst.

The model never sees raw workbooks - it works through tools over the tidy datasets, so every number it states
comes from a query it ran. Every table and chart it produces is attached to the answer, which is itself a
`Report` and therefore exportable to HTML / PDF / PPTX / CSV like any dashboard.

Model: Claude (Anthropic API) with a manual tool loop. Server-side refusal fallbacks are on
(`fallbacks="default"`), the system prompt + tool list are prompt-cached, and with no API key (or if the call
fails) the offline analyst (`offline_answer`) routes the question to the right dashboard / query instead.
"""
from __future__ import annotations

import difflib
import json
import re
import traceback
from dataclasses import dataclass, field

import pandas as pd

from . import analytics as A
from .charts import KINDS, ChartSpec, from_df
from .model import CATALOG, OEM_LABEL, OEMS, TARGETS
from .report import Report, display_df

DEFAULT_MODEL = "claude-opus-5-5"
# Free / OpenAI-compatible providers (tool calling supported). Gemini: free key at aistudio.google.com/apikey.
PROVIDERS = {
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "model": "gemini-flash-latest"},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "model": "meta-llama/llama-3.3-70b-instruct:free"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MAX_ROWS_TO_MODEL = 40
MAX_STEPS = 12


# --------------------------------------------------------------------------- #
# tool implementations (pure pandas, no eval)
# --------------------------------------------------------------------------- #
OPS = {"=", "!=", ">", ">=", "<", "<=", "in", "not in", "contains", "startswith", "between"}
AGGS = {"sum", "mean", "count", "nunique", "min", "max", "median"}


@dataclass
class Workspace:
    pack: object
    results: dict = field(default_factory=dict)      # id -> DataFrame
    charts: list = field(default_factory=list)       # ChartSpec
    attached: list = field(default_factory=list)     # ("table", title, df) | ("chart", spec) | ("report", Report)
    log: list = field(default_factory=list)

    # ---- tools -------------------------------------------------------------
    def list_datasets(self) -> dict:
        out = []
        for k, desc in CATALOG.items():
            df = self.pack.get(k)
            if df.empty: continue
            out.append({"name": k, "rows": len(df), "columns": list(df.columns), "about": desc})
        return {"datasets": out, "as_of": self.pack.asof, "targets": TARGETS}

    def describe_dataset(self, name: str) -> dict:
        df = self.pack.get(name)
        if df.empty: return {"error": f"unknown or empty dataset '{name}'", "available": [k for k in CATALOG if not self.pack.get(k).empty]}
        cols = []
        for c in df.columns:
            s = df[c]
            info = {"column": c, "dtype": str(s.dtype), "non_null": int(s.notna().sum())}
            if pd.api.types.is_numeric_dtype(s) and s.dtype != bool:
                info.update(min=_j(s.min()), max=_j(s.max()), sum=_j(s.sum()))
            else:
                vc = s.astype(str).value_counts()
                info.update(distinct=int(len(vc)), top=[f"{k} ({v})" for k, v in vc.head(12).items()])
            cols.append(info)
        return {"name": name, "rows": len(df), "about": CATALOG.get(name, ""), "columns": cols}

    def query(self, dataset: str, filters: list | None = None, group_by: list | None = None, metrics: list | None = None,
              ratios: list | None = None, sort_by: str | None = None, ascending: bool = False, limit: int = 25,
              title: str = "", show: bool = True, columns: list | None = None) -> dict:
        df = self.pack.get(dataset)
        if df.empty: return {"error": f"unknown or empty dataset '{dataset}'"}
        d = df
        for f in filters or []:
            d = _apply_filter(d, f)
        if group_by or metrics:
            gb = [g for g in (group_by or []) if g in d.columns]
            mets = metrics or [{"column": "base", "agg": "sum"}, {"column": "ach", "agg": "sum"}]
            spec = {}
            for m in mets:
                col, agg = m.get("column"), (m.get("agg") or "sum").lower()
                if agg not in AGGS: return {"error": f"agg must be one of {sorted(AGGS)}"}
                if col not in d.columns and agg != "count": return {"error": f"column '{col}' not in {dataset}: {list(d.columns)}"}
                name = m.get("as") or (f"{col}_{agg}" if agg != "sum" else col)
                spec[name] = (col if col in d.columns else d.columns[0], "size" if agg == "count" else agg)
            r = d.groupby(gb, dropna=False).agg(**spec).reset_index() if gb else pd.DataFrame({k: [getattr(d[c], a)() if a != "size" else len(d)]
                                                                                              for k, (c, a) in spec.items()})
        else:
            r = d[[c for c in (columns or d.columns) if c in d.columns]]
        for rt in ratios or []:
            num, den = rt.get("numerator"), rt.get("denominator")
            if num in r.columns and den in r.columns:
                r[rt.get("name", f"{num}/{den}")] = [a / b if b else None for a, b in zip(r[num], r[den])]
        if sort_by and sort_by in r.columns:
            r = r.sort_values(sort_by, ascending=ascending)
        total = len(r)
        r = r.head(max(1, min(int(limit or 25), 500)))
        rid = f"r{len(self.results) + 1}"
        self.results[rid] = r
        if show: self.attached.append(("table", title or f"{dataset} query", r))
        self.log.append(f"query {dataset} -> {rid} ({total} rows)")
        return {"result_id": rid, "rows_total": total, "rows": _records(r.head(MAX_ROWS_TO_MODEL)),
                "note": "Percent/ratio columns are fractions (0.45 = 45%)."}

    def make_chart(self, result_id: str, kind: str, x: str, y: list | str, title: str = "", target: float | None = None) -> dict:
        r = self.results.get(result_id)
        if r is None: return {"error": f"unknown result_id {result_id}; run query first"}
        if kind not in KINDS: return {"error": f"kind must be one of {KINDS}"}
        spec = from_df(r, x, y, kind, title, target=target)
        if spec is None: return {"error": f"columns not found; available: {list(r.columns)}"}
        self.attached.append(("chart", spec))
        return {"chart": "attached", "title": spec.title}

    def dashboard(self, name: str, oem: str | None = None, zm: str | None = None, rm: str | None = None,
                  dealer_code: str | None = None) -> dict:
        p = self.pack
        name = (name or "").lower()
        oem = _canon_oem(oem) if oem else None
        zm = self._canon_person(zm, "zm") if zm else None
        rm = self._canon_person(rm, "rm") if rm else None
        if name == "overview": rep = A.overview_report(p)
        elif name in ("oem", "brand"): rep = A.oem_report(p, oem or "ALL", zm, rm)
        elif name in ("royal_enfield", "re"): rep = A.re_report(p, zm, rm)
        elif name == "dealer": rep = A.dealer_report(p, dealer_code or "")
        elif name == "data_quality": rep = A.data_quality_report(p)
        elif name in ("rm_review", "zm_review"):
            who = rm or zm or ""
            rep = Report(f"{'RM' if name == 'rm_review' else 'ZM'} review - {who}")
            for o in OEMS:
                sub = A.oem_report(p, o, zm=who if name == "zm_review" else None, rm=who if name == "rm_review" else None)
                if len(sub.blocks) > 1: rep.h(sub.title); rep.extend(sub)
        else:
            from . import ra
            ids = [r[0] for r in ra.REPORTS if r[0] not in ("rm", "dealer")]
            if name not in ids:
                return {"error": "name must be overview | oem | royal_enfield | dealer | rm_review | zm_review | data_quality | " + " | ".join(ids)}
            rep = ra.build(p, name, zm, dealer_code)
        self.attached.append(("report", rep))
        return {"report": rep.title, "summary": _summarize(rep)}

    def find(self, text: str) -> dict:
        """Fuzzy-match dealer names/codes, RMs and ZMs across all datasets."""
        t = (text or "").strip().upper()
        hits = []
        seen = set()
        for tb in ("retention", "re_dealer_snapshot", "pc_penetration_month", "re_dealer_master"):
            df = self.pack.get(tb)
            if df.empty or "dealer_code" not in df: continue
            for dc, nm in df[["dealer_code", "dealer_name"]].drop_duplicates().itertuples(index=False):
                key = (dc, nm)
                if key in seen: continue
                if t == str(dc).upper() or t in str(nm).upper():
                    seen.add(key); hits.append({"type": "dealer", "code": dc, "name": nm, "source": tb})
        for role in ("rm", "zm"):
            names = self._people(role)
            for m in difflib.get_close_matches(t, names, n=3, cutoff=.6) + [x for x in names if t in x]:
                if (role, m) not in seen:
                    seen.add((role, m)); hits.append({"type": role, "name": m})
        return {"matches": hits[:25]}

    # ---- helpers -----------------------------------------------------------
    def _people(self, role):
        out = set()
        for tb in ("retention", "re_dealer_snapshot", "pc_penetration_month", "re_visits", "re_dealer_master"):
            df = self.pack.get(tb)
            if not df.empty and role in df: out |= set(df[role].dropna().astype(str))
        return sorted(x for x in out if x)

    def _canon_person(self, name, role):
        names = self._people(role)
        u = name.strip().upper()
        if u in names: return u
        m = difflib.get_close_matches(u, names, n=1, cutoff=.5)
        return m[0] if m else u


def _apply_filter(d: pd.DataFrame, f: dict) -> pd.DataFrame:
    col, op, val = f.get("column"), (f.get("op") or "=").lower(), f.get("value")
    if col not in d.columns or op not in OPS: return d
    s = d[col]
    if isinstance(val, str) and s.dtype == object:
        sv, v = s.astype(str).str.upper(), val.upper()
    else:
        sv, v = s, val
    if op == "=": return d[sv == v]
    if op == "!=": return d[sv != v]
    if op == ">": return d[s > val]
    if op == ">=": return d[s >= val]
    if op == "<": return d[s < val]
    if op == "<=": return d[s <= val]
    if op in ("in", "not in"):
        vals = [str(x).upper() for x in (val if isinstance(val, list) else [val])]
        m = s.astype(str).str.upper().isin(vals)
        return d[m] if op == "in" else d[~m]
    if op == "contains": return d[s.astype(str).str.upper().str.contains(str(val).upper(), regex=False)]
    if op == "startswith": return d[s.astype(str).str.upper().str.startswith(str(val).upper())]
    if op == "between" and isinstance(val, list) and len(val) == 2: return d[(s >= val[0]) & (s <= val[1])]
    return d


def _j(v):
    if isinstance(v, (pd.Timestamp,)): return str(v.date())
    try:
        f = float(v); return round(f, 4)
    except (TypeError, ValueError):
        return str(v)


def _records(df: pd.DataFrame) -> list:
    return [{k: _j(v) if not isinstance(v, str) else v for k, v in r.items()} for r in df.to_dict("records")]


def _canon_oem(o: str) -> str:
    u = (o or "").upper().strip()
    for k in OEMS + ["ALL"]:
        if u == k or u == OEM_LABEL.get(k, "").upper(): return k
    if u in ("VW",): return "VOLKSWAGEN"
    if u in ("RE", "ROYAL"): return "ROYAL ENFIELD"
    return u


def _summarize(rep: Report, limit=2500) -> str:
    parts = []
    for b in rep.blocks:
        if b.kind == "kpis": parts.append("; ".join(f"{k[0]}: {k[1]} ({k[2]})" if len(k) > 2 and k[2] else f"{k[0]}: {k[1]}" for k in b.items))
        elif b.kind == "bullets": parts += [str(i).replace("**", "") for i in b.items]
        elif b.kind == "heading": parts.append(f"## {b.title}")
        elif b.kind == "table" and b.df is not None:
            parts.append(f"[table {b.title}: {len(b.df)} rows] " + display_df(b.df.head(5)).to_csv(index=False)[:400])
    s = "\n".join(parts)
    return s[:limit]


# --------------------------------------------------------------------------- #
# tool schemas (kept shallow: some providers reject deeply nested schemas)
# --------------------------------------------------------------------------- #
TOOLS = [
    {"name": "list_datasets", "description": "List datasets with row counts, columns and meaning; also as-of dates and targets.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "describe_dataset", "description": "Column types, numeric ranges and the most common values of one dataset.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "query", "description":
        "Filter / group / aggregate a dataset. filters: list of {column, op, value}; op in = != > >= < <= in 'not in' contains "
        "startswith between. metrics: list of {column, agg, as}; agg in sum mean count nunique min max median. "
        "ratios: list of {name, numerator, denominator} computed on the aggregated columns (use for retention % = ach/base). "
        "The result table is attached to the answer unless show=false.",
     "parameters": {"type": "object", "properties": {
         "dataset": {"type": "string"}, "filters": {"type": "array", "items": {"type": "object"}},
         "group_by": {"type": "array", "items": {"type": "string"}}, "metrics": {"type": "array", "items": {"type": "object"}},
         "ratios": {"type": "array", "items": {"type": "object"}}, "sort_by": {"type": "string"},
         "ascending": {"type": "boolean"}, "limit": {"type": "integer"}, "title": {"type": "string"},
         "show": {"type": "boolean"}, "columns": {"type": "array", "items": {"type": "string"}}},
        "required": ["dataset"]}},
    {"name": "make_chart", "description": "Chart a query result. kind: bar | hbar | line | stacked | pie | scatter. "
                                          "y: one or more numeric columns on the SAME scale (never mix counts and % - "
                                          "make two charts). target: optional reference line (fraction for %).",
     "parameters": {"type": "object", "properties": {
         "result_id": {"type": "string"}, "kind": {"type": "string"}, "x": {"type": "string"},
         "y": {"type": "array", "items": {"type": "string"}}, "title": {"type": "string"}, "target": {"type": "number"}},
        "required": ["result_id", "kind", "x", "y"]}},
    {"name": "dashboard", "description": "Run a ready-made analysis and attach it (it exports like any report): overview (all OEMs) | "
                                         "oem (oem = AUDI, SKODA, VOLKSWAGEN, VOLVO, PORSCHE or ALL; optional zm / rm) | royal_enfield "
                                         "(optional zm / rm) | dealer (dealer_code) | rm_review (rm) | zm_review (zm) | data_quality | "
                                         "zone (zone executive page) | boom (daily stand-up pack, needs zm) | under (underperformers with "
                                         "peer proof) | fy (first vs other year) | movers | volvo | re | open (Volvo open cases) | groups "
                                         "(consolidated dealer groups) | meeting (dealer-facing meeting deck, needs dealer_code) | "
                                         "penetration | payout | dealers | rms (RM scorecards) | quality. Optional zm scopes any of them. "
                                         "Returns its KPIs and insights as text.",
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string"}, "oem": {"type": "string"}, "zm": {"type": "string"}, "rm": {"type": "string"},
         "dealer_code": {"type": "string"}}, "required": ["name"]}},
    {"name": "find", "description": "Fuzzy search for a dealer (name or code), RM or ZM before filtering on it.",
     "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
]


def system_prompt(pack) -> str:
    return f"""You are Claude, working as the MIS analyst for an insurance-broking retention team (EIBL/ABIBL "EDME" field teams). You answer
questions about dealer insurance renewals, penetration, AI calling, incentives, field visits and payouts for these OEM books:
Royal Enfield (two-wheeler RESP programme) and private cars - Audi, Skoda, Volkswagen, Porsche (SAVWIPL) and Volvo.

Data as of: {json.dumps(pack.asof)}. Targets (fractions, editable): {json.dumps(TARGETS)}.
Hierarchy: dealer -> RM (relationship manager) -> ZM (zonal manager). FY = first-year renewal (customer's first renewal),
OY = other-year (second and later). base = policies due, ach = renewed. Retention % = sum(ach)/sum(base) - never average
percentages. The running month is month-to-date (is_mtd=True): say so and compare it with the same date last month, not
with closed months. RE dealer-level monthly history covers the North zone only; RE national figures are in re_region /
re_dealer_snapshot. Penetration (policies at vehicle sale / retail) and retention are different books - never add them.
AI calling Conversions% is conversions / disposed calls, not / base.

Rules:
1. Every number you state must come from a tool result in this conversation. Never guess or invent.
2. Start with `dashboard` for broad questions (an OEM, a zone, an RM, a dealer, "overview"); use `query` for specific
   cuts, rankings, comparisons and anything the dashboards don't cover; use `find` first when a name may be misspelt.
3. Add a chart (make_chart) whenever a comparison or trend is easier to see than to read.
4. Answer in concise markdown: a one-line headline with the key number, 3-6 bullets of insight (what, why, who), then
   "Recommended actions" with 2-4 specific, owner-named actions. Tables and charts you produced are attached
   automatically - refer to them, do not repeat them in full.
5. Never output customer-level personal data. Dealer, RM and ZM names are fine - but name dealers only when the
   question is about dealers (asks which / who / list / top / worst / a named dealer); otherwise describe them
   anonymously ("a Skoda dealer in Naman Singh's zone").
6. When asked for a report, deck, pack or file, build it with the matching `dashboard` report (or queries + charts)
   and say it is attached - the viewer exports it as HTML, PDF, PowerPoint or CSV from the answer.
7. Indian number formatting is welcome (lakh / crore) for money; show percentages with one decimal."""


# --------------------------------------------------------------------------- #
# the loop
# --------------------------------------------------------------------------- #
@dataclass
class Answer:
    text: str
    report: Report
    steps: list
    error: str | None = None


def run_agent(pack, question: str, history: list | None = None, api_key: str = "", model: str = "",
              effort: str = "high", on_step=None, provider: str = "claude") -> Answer:
    if not api_key:
        return offline_answer(pack, question)
    if provider in PROVIDERS:
        ws = Workspace(pack)
        try:
            cfg = PROVIDERS[provider]
            text = _loop_openai(ws, question, history or [], api_key, model or cfg["model"], cfg["base_url"], on_step, provider)
        except Exception as e:
            msg = str(e)
            why = ("the API key was rejected - check it in Data & settings" if "401" in msg or "403" in msg or "API key" in msg
                   else "the free quota is used up for now - wait a minute or switch provider" if "429" in msg
                   else f"{type(e).__name__}: {msg[:160]}")
            return _fallback(pack, question, why)
        return Answer(text, build_answer_report(question, text, ws), ws.log)
    import anthropic
    ws = Workspace(pack)
    try:
        text = _loop_claude(ws, question, history or [], api_key, model or DEFAULT_MODEL, effort, on_step)
    except anthropic.AuthenticationError:
        return _fallback(pack, question, "the Anthropic API key was rejected - check it in Settings")
    except anthropic.PermissionDeniedError:
        return _fallback(pack, question, "the API key has no access to this model")
    except anthropic.RateLimitError:
        return _fallback(pack, question, "Claude is rate-limited right now - try again in a minute")
    except anthropic.APIStatusError as e:
        return _fallback(pack, question, f"the Claude API returned {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        return _fallback(pack, question, "could not reach the Claude API")
    return Answer(text, build_answer_report(question, text, ws), ws.log)


def _fallback(pack, question, why) -> Answer:
    ans = offline_answer(pack, question)
    ans.error = why
    ans.text = f"_Claude is unavailable ({why}); showing the built-in analysis instead._\n\n" + ans.text
    return ans


def _dispatch(ws: Workspace, name: str, args: dict) -> dict:
    fn = {"list_datasets": ws.list_datasets, "describe_dataset": ws.describe_dataset, "query": ws.query,
          "make_chart": ws.make_chart, "dashboard": ws.dashboard, "find": ws.find}.get(name)
    if not fn: return {"error": f"unknown tool {name}"}
    try:
        return fn(**(args or {}))
    except TypeError as e:
        return {"error": f"bad arguments for {name}: {e}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc(limit=1)}


def pick_model(ids: list[str], provider: str) -> str | None:
    """Newest suitable chat model from the provider's own list (model names retire; never trust a hard-coded one)."""
    import re
    bad = re.compile(r"image|tts|audio|live|embed|vision|robotics|computer|learnlm|gemma|imagen|veo|aqa|guard|whisper|"
                     r"distil|preview-\d|exp|thinking", re.I)
    want = {"gemini": r"flash", "groq": r"llama-3\.3-70b|gpt-oss-120b|llama-4-maverick", "openrouter": r".", "openai": r"gpt-4o-mini|gpt-4\.1-mini|gpt-5-mini"}
    rx = re.compile(want.get(provider, "."), re.I)
    def score(i):
        m = re.search(r"(\d+(?:\.\d+)?)", i)
        ver = float(m.group(1)) if m else 0.0
        lite = 1 if "lite" in i.lower() else 0
        prev = 1 if re.search(r"preview|beta", i, re.I) else 0
        return (lite, prev, -ver, len(i))
    cands = [i for i in ids if rx.search(i) and not bad.search(i)]
    return sorted(cands, key=score)[0].removeprefix("models/") if cands else None


def _loop_openai(ws, question, history, api_key, model, base_url, on_step, provider="gemini"):
    from openai import NotFoundError, OpenAI
    client = OpenAI(api_key=api_key, base_url=base_url)
    if not model or model == PROVIDERS.get(provider, {}).get("model"):
        try:
            model = pick_model([m.id for m in client.models.list()], provider) or model
        except Exception:
            pass
    tools = [{"type": "function", "function": t} for t in TOOLS]
    msgs = [{"role": "system", "content": system_prompt(ws.pack)}]
    msgs += [{"role": h["role"], "content": h["content"]} for h in history[-6:] if h.get("content")]
    msgs.append({"role": "user", "content": question})
    for _ in range(MAX_STEPS):
        try:
            r = client.chat.completions.create(model=model, messages=msgs, tools=tools, temperature=0.2)
        except NotFoundError:            # model retired for this account -> discover a live one once and retry
            alt = pick_model([m.id for m in client.models.list()], provider)
            if not alt or alt == model: raise
            model = alt
            r = client.chat.completions.create(model=model, messages=msgs, tools=tools, temperature=0.2)
        m = r.choices[0].message
        calls = m.tool_calls or []
        if not calls:
            return m.content or ""
        msgs.append({"role": "assistant", "content": m.content or "", "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}} for c in calls]})
        for c in calls:
            try: args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError: args = {}
            if on_step: on_step(c.function.name, args)
            res = _dispatch(ws, c.function.name, args)
            ws.log.append(f"{c.function.name}({json.dumps(args)[:200]})")
            msgs.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps(res, default=str)[:12000]})
    return "_I reached the analysis step limit - the tables and charts gathered so far are attached._"


def _loop_claude(ws, question, history, api_key, model, effort, on_step):
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    tools = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in TOOLS]
    # tools + system are identical on every call -> cached prefix (tools render before system)
    system = [{"type": "text", "text": system_prompt(ws.pack), "cache_control": {"type": "ephemeral"}}]
    msgs = [{"role": h["role"], "content": h["content"]} for h in history[-8:] if h.get("content")]
    msgs.append({"role": "user", "content": question})
    text_so_far = ""
    for _ in range(MAX_STEPS):
        r = client.beta.messages.create(
            model=model, max_tokens=16000, system=system, tools=tools, messages=msgs,
            output_config={"effort": effort if effort in EFFORTS else "high"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        text = "".join(b.text for b in r.content if b.type == "text")
        text_so_far = text or text_so_far
        if r.stop_reason == "refusal":
            return "Claude declined to answer this request. Try rephrasing it as a question about the MIS data."
        uses = [b for b in r.content if b.type == "tool_use"]
        if r.stop_reason != "tool_use" or not uses:
            return text or text_so_far or "(no answer)"
        msgs.append({"role": "assistant", "content": r.content})   # keep every block, thinking included
        results = []
        for u in uses:
            if on_step: on_step(u.name, u.input)
            res = _dispatch(ws, u.name, u.input or {})
            ws.log.append(f"{u.name}({json.dumps(u.input)[:200]})")
            results.append({"type": "tool_result", "tool_use_id": u.id, "is_error": "error" in res,
                            "content": json.dumps(res, default=str)[:12000]})
        msgs.append({"role": "user", "content": results})           # all results in one message
    return (text_so_far + "\n\n" if text_so_far else "") + "_I reached the analysis step limit - the tables and charts gathered so far are attached._"


def build_answer_report(question: str, text: str, ws: Workspace) -> Report:
    rep = Report(_title_from(question), "Answer from the MIS agent")
    rep.extend(md_to_report(text))
    for item in ws.attached:
        if item[0] == "table": rep.table(item[2], item[1])
        elif item[0] == "chart": rep.chart(item[1])
        elif item[0] == "report":
            rep.h(item[1].title); rep.extend(item[1])
    return rep


def _title_from(q: str) -> str:
    q = re.sub(r"\s+", " ", q).strip().rstrip("?")
    return (q[:1].upper() + q[1:])[:80] if q else "MIS answer"


def md_to_report(md: str) -> Report:
    """Markdown (headings, bullets, pipe tables, paragraphs) -> report blocks, so any answer exports cleanly."""
    rep = Report("")
    lines = (md or "").splitlines()
    i, para, bullets = 0, [], []

    def flush():
        nonlocal para, bullets
        if para: rep.p(" ".join(para)); para = []
        if bullets: rep.bullets(bullets); bullets = []

    while i < len(lines):
        ln = lines[i].rstrip()
        if re.match(r"^\s*\|.*\|\s*$", ln) and i + 1 < len(lines) and re.match(r"^\s*\|[\s:\-|]+\|\s*$", lines[i + 1]):
            flush()
            hdr = [c.strip() for c in ln.strip().strip("|").split("|")]
            rows = []; i += 2
            while i < len(lines) and re.match(r"^\s*\|.*\|\s*$", lines[i]):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")]); i += 1
            rep.table(pd.DataFrame([r[:len(hdr)] + [""] * (len(hdr) - len(r)) for r in rows], columns=hdr))
            continue
        m = re.match(r"^\s*#{1,4}\s+(.*)", ln)
        if m: flush(); rep.h(m.group(1).strip()); i += 1; continue
        m = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)", ln)
        if m:
            if para: rep.p(" ".join(para)); para = []
            bullets.append(m.group(1).strip()); i += 1; continue
        if not ln.strip(): flush(); i += 1; continue
        if bullets: rep.bullets(bullets); bullets = []
        para.append(ln.strip()); i += 1
    flush()
    return rep


# --------------------------------------------------------------------------- #
# offline analyst (no API key / model failure)
# --------------------------------------------------------------------------- #
def offline_answer(pack, question: str) -> Answer:
    ws = Workspace(pack)
    q = question.lower()
    oem = next((o for o in OEMS if o.lower() in q or OEM_LABEL[o].lower() in q), None)
    if re.search(r"\b(vw|volks)", q): oem = "VOLKSWAGEN"
    if re.search(r"\b(re|resp|royal|bike|two.?wheeler)\b", q): oem = "ROYAL ENFIELD"
    zm = next((z for z in ws._people("zm") if z.lower() in q), None)
    rm = next((r for r in ws._people("rm") if r.lower() in q), None)
    code_m = re.search(r"\b([A-Z]{2}\d{4,6}|6IN\d{4}|\d{3,6})\b", question.upper())
    notes = []
    if code_m and re.search(r"dealer|code|review|report|\bof\b", q) and not re.search(r"top|bottom|worst|best", q):
        ws.dashboard("dealer", dealer_code=code_m.group(1))
    elif any(k in q for k in ("penetration", "retail", "new policy", "new policies")):
        if oem == "ROYAL ENFIELD":
            ws.dashboard("royal_enfield", zm=zm, rm=rm)
        else:
            rep = A.penetration_section(pack, oem or "ALL", zm, rm)
            ws.attached.append(("report", Report("Penetration", "").extend(rep)))
    elif any(k in q for k in ("payout", "utr", "invoice", "payment")):
        t = "re_payouts" if oem == "ROYAL ENFIELD" else "pc_payouts"
        ws.attached.append(("report", Report("Payouts", "").extend(A.payout_section(pack, t, None if oem in (None, "ROYAL ENFIELD") else oem, zm, rm))))
    elif any(k in q for k in ("not renewed", "open case", "chase", "lost")):
        ws.attached.append(("report", Report("Volvo open cases", "").extend(A.volvo_open_section(pack, zm, rm))))
    elif any(k in q for k in ("quality", "source", "file", "sheet")):
        ws.dashboard("data_quality")
    elif oem == "ROYAL ENFIELD" or any(k in q for k in ("ai call", "incentive", "visit", "focus", "edme")):
        ws.dashboard("royal_enfield", zm=zm, rm=rm)
    elif oem or zm or rm:
        ws.dashboard("oem", oem=oem or "ALL", zm=zm, rm=rm)
    else:
        ws.dashboard("overview")
        notes.append("Tip: name an OEM, ZM, RM or dealer code to go deeper.")
    rep = build_answer_report(question, "", ws)
    bl = [b for b in rep.blocks if b.kind == "bullets"]
    kp = [b for b in rep.blocks if b.kind == "kpis"]
    text = "\n".join(f"- {i}" for i in (bl[0].items if bl else []))
    if not text and kp:
        text = "\n".join(f"- **{k[0]}**: {k[1]}" + (f" ({k[2]})" if len(k) > 2 and k[2] else "") for k in kp[0].items)
    text = text or "Here is the relevant analysis."
    if notes: text += "\n\n" + "\n".join(notes)
    return Answer(text, rep, ws.log)
